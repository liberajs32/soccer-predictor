"""FastAPI backend: serves upcoming fixtures and 1X2 predictions.

Run locally with:
    uvicorn api.main:app --reload --port 8000
"""
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "model"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from db import get_connection, upsert_matches
from elo import EloModel, outcome_label
from ensemble import blend_probs
from poisson import PoissonModel
from betexplorer import LEAGUE_URL_SEGMENTS, fetch_fixtures, fetch_season

app = FastAPI(title="Soccer 1X2 Predictor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # personal/local project; tighten if ever deployed publicly
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# The underlying training data only changes once a day (the scheduled scrape
# workflow), so there's no reason to ever refit more than once a day -- a
# short TTL just means Render's slow/contended free-tier CPU pays the
# refit cost (observed 1-2s locally, sometimes 90s+ on Render) far more
# often than the data actually warrants.
CACHE_TTL_SECONDS = 24 * 60 * 60
_model_cache: dict[str, tuple[float, EloModel, PoissonModel]] = {}

# One connection reused for the life of the process. Each get_connection()
# call spins up a new client (for Turso, a background thread + HTTP session),
# so calling it fresh per-request would leak resources over time.
_conn = None


def _db():
    global _conn
    if _conn is None:
        _conn = get_connection()
    return _conn


class Fixture(BaseModel):
    id: int
    date: Optional[str]
    round: Optional[str]
    home_team: str
    away_team: str
    odds_home: Optional[float]
    odds_draw: Optional[float]
    odds_away: Optional[float]


class Prediction(Fixture):
    league: str
    elo_prob_home: float
    elo_prob_draw: float
    elo_prob_away: float
    poisson_prob_home: float
    poisson_prob_draw: float
    poisson_prob_away: float
    ensemble_prob_home: float
    ensemble_prob_draw: float
    ensemble_prob_away: float
    predicted_outcome: str  # from the ensemble blend, "H"/"D"/"A"
    predicted_probability: float  # max(ensemble_prob_*) -- how confident the pick is


class ComboLeg(BaseModel):
    league: str
    date: Optional[str]
    home_team: str
    away_team: str
    predicted_outcome: str
    predicted_probability: float
    odds: Optional[float]  # market odds for predicted_outcome, if posted yet


class Combo(BaseModel):
    legs: list[ComboLeg]
    combined_probability: float


def _market_probs(m: dict) -> tuple[float, float, float] | None:
    if not (m.get("odds_home") and m.get("odds_draw") and m.get("odds_away")):
        return None
    inv = [1.0 / m["odds_home"], 1.0 / m["odds_draw"], 1.0 / m["odds_away"]]
    total = sum(inv)
    return tuple(x / total for x in inv)


def _require_league(league: str) -> str:
    if league not in LEAGUE_URL_SEGMENTS:
        raise HTTPException(status_code=404, detail=f"unknown league '{league}', choose one of {sorted(LEAGUE_URL_SEGMENTS)}")
    return league


def _fetch_upcoming(conn, league: str) -> list[dict]:
    # A handful of archived matches (e.g. K League 1 2020, disrupted by
    # COVID-era scheduling changes) sit in the DB with no score and no
    # future date -- they were never played and never will be, so a plain
    # "score IS NULL" filter would surface them as "upcoming" fixtures.
    # Requiring date >= today excludes that never-happened backlog.
    rows = conn.execute(
        """
        SELECT id, date, round, home_team, away_team, odds_home, odds_draw, odds_away
        FROM matches
        WHERE league = ? AND home_score IS NULL AND date >= date('now')
        ORDER BY date ASC
        """,
        (league,),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_decided(conn, league: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT date, home_team, away_team, home_score, away_score
        FROM matches
        WHERE league = ? AND date IS NOT NULL AND home_score IS NOT NULL
        ORDER BY date ASC, id ASC
        """,
        (league,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_models(league: str) -> tuple[EloModel, PoissonModel]:
    cached = _model_cache.get(league)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1], cached[2]

    decided = _fetch_decided(_db(), league)

    elo = EloModel()
    pairs = []
    for m in decided:
        gap = elo.rating_gap(m["home_team"], m["away_team"])
        pairs.append((gap, outcome_label(m["home_score"], m["away_score"])))
        elo.update(m["home_team"], m["away_team"], m["home_score"], m["away_score"])
    elo.fit_outcome_mapping(pairs)

    poisson = PoissonModel()
    if decided:
        poisson.fit(decided)

    _model_cache[league] = (time.monotonic(), elo, poisson)
    return elo, poisson


@app.get("/leagues")
def leagues() -> list[str]:
    return sorted(LEAGUE_URL_SEGMENTS)


@app.get("/fixtures", response_model=list[Fixture])
def fixtures(league: str = Query(...)):
    _require_league(league)
    return _fetch_upcoming(_db(), league)


def _predict_league(league: str, max_date: str | None = None) -> list[Prediction]:
    upcoming = _fetch_upcoming(_db(), league)
    if max_date:
        # Predicting is real compute (Poisson/Elo predict_proba per match),
        # not free even with a warm model cache -- a full EPL season is 380
        # fixtures. /combo only needs the handful of near-term matches, so
        # filter before predicting rather than after.
        upcoming = [m for m in upcoming if m["date"] and m["date"] <= max_date]
    if not upcoming:
        return []

    elo, poisson = _get_models(league)

    results = []
    for m in upcoming:
        elo_probs = elo.predict_proba(m["home_team"], m["away_team"])
        poisson_probs = poisson.predict_proba(m["home_team"], m["away_team"])
        ensemble_probs = blend_probs(elo_probs, poisson_probs, _market_probs(m))
        best_idx = ensemble_probs.index(max(ensemble_probs))
        predicted = ["H", "D", "A"][best_idx]
        results.append(
            Prediction(
                **m,
                league=league,
                elo_prob_home=elo_probs[0],
                elo_prob_draw=elo_probs[1],
                elo_prob_away=elo_probs[2],
                poisson_prob_home=poisson_probs[0],
                poisson_prob_draw=poisson_probs[1],
                poisson_prob_away=poisson_probs[2],
                ensemble_prob_home=ensemble_probs[0],
                ensemble_prob_draw=ensemble_probs[1],
                ensemble_prob_away=ensemble_probs[2],
                predicted_outcome=predicted,
                predicted_probability=ensemble_probs[best_idx],
            )
        )
    return results


@app.get("/predictions", response_model=list[Prediction])
def predictions(league: str = Query(...)):
    _require_league(league)
    return _predict_league(league)


COMBO_WINDOW_DAYS = 10


@app.get("/combo", response_model=Combo)
def combo(legs: int = Query(2, ge=2, le=4), outcome: Optional[str] = Query(None, pattern="^[HDA]$")):
    """Recommend a same-day-actionable combo, restricted to matches in the
    next COMBO_WINDOW_DAYS so it's something you could actually bet on now
    rather than a lopsided fixture months out that hasn't even got odds yet.

    Legs are drawn from a single calendar date, but can mix leagues (a K1
    pick and a K2 pick on the same day are fine) -- "round" isn't comparable
    across leagues (K1's round 19 vs BL1's round 3 mean nothing next to each
    other), so date is the league-agnostic stand-in for "this is one
    coherent slate of matches." Every date's candidates are scored and the
    best date wins.

    Without `outcome`: ranks by each match's own best-outcome probability --
    may mix home/draw/away picks within the winning date.

    With `outcome=H`/`D`/`A`: forces every leg to that outcome (e.g. `D` for
    a "draw-draw" combo) and ranks by that outcome's probability specifically,
    even on matches where it isn't the single most likely result -- draws pay
    out more, so "most likely draw" and "most likely overall" are different
    questions."""
    cutoff = (date.today() + timedelta(days=COMBO_WINDOW_DAYS)).isoformat()

    all_predictions: list[Prediction] = []
    for league in LEAGUE_URL_SEGMENTS:
        all_predictions.extend(_predict_league(league, max_date=cutoff))

    if outcome:
        prob_field = {"H": "ensemble_prob_home", "D": "ensemble_prob_draw", "A": "ensemble_prob_away"}[outcome]
        odds_field = {"H": "odds_home", "D": "odds_draw", "A": "odds_away"}[outcome]
        scored = [(p, getattr(p, prob_field), getattr(p, odds_field), outcome) for p in all_predictions]
    else:
        outcome_odds_field = {"H": "odds_home", "D": "odds_draw", "A": "odds_away"}
        scored = [
            (p, p.predicted_probability, getattr(p, outcome_odds_field[p.predicted_outcome]), p.predicted_outcome)
            for p in all_predictions
        ]

    # Group by calendar date, not (league, round): round numbers aren't
    # comparable across leagues (K1's round 19 means nothing next to BL1's
    # round 3), but "these matches are all being played the same day" is a
    # league-agnostic way to keep a combo temporally coherent while still
    # allowing K1+K2+BL1 legs to mix.
    groups: dict[str, list] = {}
    for c in scored:
        key = c[0].date or ""
        groups.setdefault(key, []).append(c)

    best: tuple[list, float] | None = None
    for group in groups.values():
        group.sort(key=lambda c: c[1], reverse=True)

        # A combo's legs are assumed independent when multiplying their
        # probabilities together -- that breaks if the same team shows up
        # twice (its current form/rotation correlates both results), so skip
        # any candidate that shares a team with a leg already picked.
        chosen = []
        used_teams: set[str] = set()
        for c in group:
            teams = {c[0].home_team, c[0].away_team}
            if teams & used_teams:
                continue
            chosen.append(c)
            used_teams |= teams
            if len(chosen) == legs:
                break

        if len(chosen) < legs:
            continue

        combined = 1.0
        for _, prob, _, _ in chosen:
            combined *= prob

        if best is None or combined > best[1]:
            best = (chosen, combined)

    if best is None:
        raise HTTPException(
            status_code=404,
            detail=f"no single round has {legs} matches with non-overlapping teams in the next {COMBO_WINDOW_DAYS} days",
        )

    top, combined = best

    return Combo(
        legs=[
            ComboLeg(
                league=p.league,
                date=p.date,
                home_team=p.home_team,
                away_team=p.away_team,
                predicted_outcome=leg_outcome,
                predicted_probability=prob,
                odds=odds,
            )
            for p, prob, odds, leg_outcome in top
        ],
        combined_probability=combined,
    )


class RefreshResult(BaseModel):
    updated_rows: int
    leagues: list[str]


REFRESH_COOLDOWN_SECONDS = 15 * 60
_last_refresh_at = 0.0


@app.post("/refresh", response_model=RefreshResult)
def refresh():
    """Manually re-scrape fixtures/odds + current-season results right now,
    for when the once-a-day schedule isn't fresh enough (e.g. checking odds
    an hour before kickoff). Rate-limited so this can't be hammered into a
    denial-of-service against BetExplorer -- it's meant to be pressed a
    handful of times a day at most, not polled."""
    global _last_refresh_at
    now = time.monotonic()
    elapsed = now - _last_refresh_at
    if elapsed < REFRESH_COOLDOWN_SECONDS:
        wait = int(REFRESH_COOLDOWN_SECONDS - elapsed)
        raise HTTPException(status_code=429, detail=f"{wait}초 후 다시 시도해주세요")
    _last_refresh_at = now

    conn = _db()
    total = 0
    for league in LEAGUE_URL_SEGMENTS:
        total += upsert_matches(conn, fetch_fixtures(league, use_cache=False))
        total += upsert_matches(conn, fetch_season(league, "current", use_cache=False))

    _model_cache.clear()  # force a refit on next request so it reflects the new data

    return RefreshResult(updated_rows=total, leagues=sorted(LEAGUE_URL_SEGMENTS))
