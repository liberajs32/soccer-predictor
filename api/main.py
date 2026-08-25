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
from ensemble import blend_probs, MODEL_VERSION
from poisson import PoissonModel
from betexplorer import LEAGUE_URL_SEGMENTS, fetch_fixtures, fetch_season

app = FastAPI(title="Soccer 1X2 Predictor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # personal/local project; tighten if ever deployed publicly
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    # CORS hides most response headers from JS by default -- Date isn't in
    # the safelisted set, and the frontend reads it to show "data as of"
    # (see App.jsx), so it needs to be explicitly exposed.
    expose_headers=["date"],
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
    round: Optional[str]
    home_team: str
    away_team: str
    predicted_outcome: str
    predicted_probability: float
    odds: Optional[float]  # market odds for predicted_outcome, if posted yet

    # Full breakdown so the UI can show a "detail view" per leg (same shape
    # as /predictions) instead of just the one highlighted outcome.
    elo_prob_home: float
    elo_prob_draw: float
    elo_prob_away: float
    poisson_prob_home: float
    poisson_prob_draw: float
    poisson_prob_away: float
    ensemble_prob_home: float
    ensemble_prob_draw: float
    ensemble_prob_away: float
    odds_home: Optional[float]
    odds_draw: Optional[float]
    odds_away: Optional[float]


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


def _log_prediction(conn, match_id: int, probs: tuple[float, float, float], predicted_outcome: str) -> None:
    """Freeze the first prediction made for this match under the current
    model version, so accuracy can be checked later against the actual
    result -- without this, /predictions only ever showed a live number
    that vanished once the match kicked off. Only logs once per
    (match, model_version): re-predicting the same still-upcoming fixture
    on every page load must not spam duplicate rows. This locks in
    whatever prediction happens to be computed first (which can be days
    before kickoff, before odds firm up) rather than the closing line --
    fine for now, revisit if that skews the accuracy numbers."""
    existing = conn.execute(
        "SELECT 1 FROM predictions WHERE match_id = ? AND model_version = ?",
        (match_id, MODEL_VERSION),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO predictions (match_id, model_version, prob_home, prob_draw, prob_away, predicted_outcome)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (match_id, MODEL_VERSION, probs[0], probs[1], probs[2], predicted_outcome),
    )
    conn.commit()


def _log_combo_pick(conn, match_id: int, legs: int, outcome: str) -> None:
    """Freeze which matches /combo actually recommended, under this exact
    (legs, outcome) config, so recommended-combo accuracy can be checked
    later against real historical picks. Only logs once per
    (match, model_version, legs, outcome): the same nearest-round match can
    show up as a combo pick on every page load until kickoff, and that must
    not spam duplicate rows."""
    existing = conn.execute(
        "SELECT 1 FROM combo_picks WHERE match_id = ? AND model_version = ? AND legs = ? AND outcome = ?",
        (match_id, MODEL_VERSION, legs, outcome),
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO combo_picks (match_id, model_version, legs, outcome) VALUES (?, ?, ?, ?)",
        (match_id, MODEL_VERSION, legs, outcome),
    )
    conn.commit()


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
        ensemble_probs = blend_probs(elo_probs, poisson_probs, _market_probs(m), league=league)
        best_idx = ensemble_probs.index(max(ensemble_probs))
        predicted = ["H", "D", "A"][best_idx]
        _log_prediction(_db(), m["id"], ensemble_probs, predicted)
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
    """Recommend a combo from each league's own nearest round (every match
    sharing that round's label, however many days it spans), pooled across
    all 4 leagues, restricted overall to COMBO_WINDOW_DAYS so it's never a
    lopsided fixture months out with no odds posted yet. Round labels aren't
    comparable across leagues (K1's round 19 vs BL1's round 3 mean nothing
    next to each other), but each league's own nearest round is still a
    real, coherent slate that's safe to combine with another league's.

    Without `outcome`: ranks by each match's own best-outcome probability --
    may mix home/draw/away picks.

    With `outcome=H`/`D`/`A`: forces every leg to that outcome (e.g. `D` for
    a "draw-draw" combo) and ranks by that outcome's probability specifically,
    even on matches where it isn't the single most likely result -- draws pay
    out more, so "most likely draw" and "most likely overall" are different
    questions."""
    cutoff = (date.today() + timedelta(days=COMBO_WINDOW_DAYS)).isoformat()

    all_predictions: list[Prediction] = []
    for league in LEAGUE_URL_SEGMENTS:
        all_predictions.extend(_predict_league(league, max_date=cutoff))

    dated = [p for p in all_predictions if p.date]
    if not dated:
        raise HTTPException(status_code=404, detail=f"no upcoming matches in the next {COMBO_WINDOW_DAYS} days")

    # For each league independently, find its own nearest round (the round
    # containing that league's earliest upcoming match) and take every
    # match in that round, however many days it spans -- a date window
    # (e.g. "next 4 days") is only a guess at a round's boundary and can
    # bleed into the *next* round once it starts, silently mixing two
    # rounds that were never actually on the same betting slip. The `round`
    # field is the real boundary. Rounds aren't comparable across leagues
    # (K1's "19. Round" and K2's "19. Round" are unrelated schedules), but
    # each league's own nearest round is still safe to combine with
    # another league's own nearest round.
    pool: list[Prediction] = []
    for league in LEAGUE_URL_SEGMENTS:
        league_matches = [p for p in dated if p.league == league]
        if not league_matches:
            continue
        nearest_round = min(league_matches, key=lambda p: p.date).round
        pool.extend(p for p in league_matches if p.round == nearest_round)

    if outcome:
        prob_field = {"H": "ensemble_prob_home", "D": "ensemble_prob_draw", "A": "ensemble_prob_away"}[outcome]
        scored = [(p, getattr(p, prob_field), outcome) for p in pool]
    else:
        scored = [(p, p.predicted_probability, p.predicted_outcome) for p in pool]

    scored.sort(key=lambda c: c[1], reverse=True)

    # A combo's legs are assumed independent when multiplying their
    # probabilities together -- that breaks if the same team shows up twice
    # (its current form/rotation correlates both results), so skip any
    # candidate that shares a team with a leg already picked.
    chosen: list[tuple[Prediction, float, str]] = []
    used_teams: set[str] = set()
    for p, prob, leg_outcome in scored:
        teams = {p.home_team, p.away_team}
        if teams & used_teams:
            continue
        chosen.append((p, prob, leg_outcome))
        used_teams |= teams
        if len(chosen) == legs:
            break

    if len(chosen) < legs:
        raise HTTPException(
            status_code=404,
            detail=(
                f"only {len(chosen)} matches with non-overlapping teams in the "
                f"{window_start}~{window_end} window, need {legs}"
            ),
        )

    combined = 1.0
    for p, prob, _ in chosen:
        combined *= prob
        _log_combo_pick(_db(), p.id, legs, outcome or "")

    return Combo(
        legs=[
            ComboLeg(
                league=p.league,
                date=p.date,
                round=p.round,
                home_team=p.home_team,
                away_team=p.away_team,
                predicted_outcome=leg_outcome,
                predicted_probability=prob,
                odds={"H": p.odds_home, "D": p.odds_draw, "A": p.odds_away}[leg_outcome],
                elo_prob_home=p.elo_prob_home,
                elo_prob_draw=p.elo_prob_draw,
                elo_prob_away=p.elo_prob_away,
                poisson_prob_home=p.poisson_prob_home,
                poisson_prob_draw=p.poisson_prob_draw,
                poisson_prob_away=p.poisson_prob_away,
                ensemble_prob_home=p.ensemble_prob_home,
                ensemble_prob_draw=p.ensemble_prob_draw,
                ensemble_prob_away=p.ensemble_prob_away,
                odds_home=p.odds_home,
                odds_draw=p.odds_draw,
                odds_away=p.odds_away,
            )
            for p, prob, leg_outcome in chosen
        ],
        combined_probability=combined,
    )


@app.get("/accuracy")
def accuracy():
    """Raw dump of every logged prediction whose match has a final score, for
    offline accuracy analysis. No aggregation here -- what's worth breaking
    out (per-league, by confidence, recent-only) changes with the question
    being asked, so keep this endpoint dumb and do the slicing client-side."""
    rows = _db().execute(
        """
        SELECT m.league, m.date, m.round, m.home_team, m.away_team,
               m.home_score, m.away_score,
               p.model_version, p.predicted_outcome,
               p.prob_home, p.prob_draw, p.prob_away, p.created_at,
               EXISTS(
                   SELECT 1 FROM combo_picks c
                   WHERE c.match_id = p.match_id AND c.model_version = p.model_version
               ) AS recommended
        FROM predictions p
        JOIN matches m ON m.id = p.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
        ORDER BY m.date ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


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
