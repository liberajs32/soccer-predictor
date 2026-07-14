"""FastAPI backend: serves upcoming fixtures and 1X2 predictions.

Run locally with:
    uvicorn api.main:app --reload --port 8000
"""
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "model"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from db import get_connection
from elo import EloModel, outcome_label
from ensemble import blend_probs
from poisson import PoissonModel
from betexplorer import LEAGUE_URL_SEGMENTS

app = FastAPI(title="Soccer 1X2 Predictor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # personal/local project; tighten if ever deployed publicly
    allow_methods=["GET"],
    allow_headers=["*"],
)

CACHE_TTL_SECONDS = 300
_model_cache: dict[str, tuple[float, EloModel, PoissonModel]] = {}


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

    conn = get_connection()
    decided = _fetch_decided(conn, league)

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
    conn = get_connection()
    return _fetch_upcoming(conn, league)


@app.get("/predictions", response_model=list[Prediction])
def predictions(league: str = Query(...)):
    _require_league(league)
    conn = get_connection()
    upcoming = _fetch_upcoming(conn, league)
    if not upcoming:
        return []

    elo, poisson = _get_models(league)

    results = []
    for m in upcoming:
        elo_probs = elo.predict_proba(m["home_team"], m["away_team"])
        poisson_probs = poisson.predict_proba(m["home_team"], m["away_team"])
        ensemble_probs = blend_probs(elo_probs, poisson_probs, _market_probs(m))
        predicted = ["H", "D", "A"][ensemble_probs.index(max(ensemble_probs))]
        results.append(
            Prediction(
                **m,
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
            )
        )
    return results
