"""Backtest the Elo and Poisson baselines against a held-out chronological
tail of matches, and compare both to the bookmaker-implied probabilities
already sitting in the odds columns.

Usage:
    python model/backtest.py --league EPL
    python model/backtest.py --league EPL K1 K2 BL1
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from elo import EloModel, outcome_label
from ensemble import blend_probs
from poisson import PoissonModel

from db import get_connection

TRAIN_FRACTION = 0.7
EPS = 1e-9


def load_decided_matches(conn, league: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT date, home_team, away_team, home_score, away_score,
               odds_home, odds_draw, odds_away
        FROM matches
        WHERE league = ? AND date IS NOT NULL
              AND home_score IS NOT NULL AND away_score IS NOT NULL
        ORDER BY date ASC, id ASC
        """,
        (league,),
    ).fetchall()
    return [dict(r) for r in rows]


def market_probs(m: dict) -> tuple[float, float, float] | None:
    if not (m["odds_home"] and m["odds_draw"] and m["odds_away"]):
        return None
    inv = [1.0 / m["odds_home"], 1.0 / m["odds_draw"], 1.0 / m["odds_away"]]
    total = sum(inv)
    return tuple(x / total for x in inv)


def log_loss(probs: tuple[float, float, float], outcome: str) -> float:
    idx = {"H": 0, "D": 1, "A": 2}[outcome]
    return -math.log(max(probs[idx], EPS))


def accuracy_hit(probs: tuple[float, float, float], outcome: str) -> bool:
    predicted = ["H", "D", "A"][probs.index(max(probs))]
    return predicted == outcome


def evaluate(name: str, predictions: list[tuple[tuple[float, float, float], str]]) -> None:
    if not predictions:
        print(f"  {name}: no predictions")
        return
    accuracy = sum(accuracy_hit(p, o) for p, o in predictions) / len(predictions)
    avg_log_loss = sum(log_loss(p, o) for p, o in predictions) / len(predictions)
    print(f"  {name:<10} accuracy={accuracy:.3f}  log_loss={avg_log_loss:.3f}  n={len(predictions)}")


def backtest_league(conn, league: str) -> None:
    matches = load_decided_matches(conn, league)
    if len(matches) < 20:
        print(f"{league}: only {len(matches)} decided matches, skipping (need more data)")
        return

    split = int(len(matches) * TRAIN_FRACTION)
    train, test = matches[:split], matches[split:]
    print(f"\n{league}: {len(train)} train / {len(test)} test matches")

    # --- Elo: run through train collecting (rating_gap, outcome) pairs, fit mapping,
    # then keep predicting+updating through the test period. ---
    elo = EloModel()
    train_pairs = []
    for m in train:
        gap = elo.rating_gap(m["home_team"], m["away_team"])
        outcome = outcome_label(m["home_score"], m["away_score"])
        train_pairs.append((gap, outcome))
        elo.update(m["home_team"], m["away_team"], m["home_score"], m["away_score"])
    elo.fit_outcome_mapping(train_pairs)

    # --- Poisson: fit once on train, predict statically on test. ---
    poisson = PoissonModel()
    poisson.fit(train)

    # Walk the test set once, computing all three models' predictions (and
    # the resulting ensemble) per match so they can be compared match-for-match.
    elo_predictions = []
    poisson_predictions = []
    market_predictions = []
    ensemble_predictions = []
    for m in test:
        outcome = outcome_label(m["home_score"], m["away_score"])

        elo_probs = elo.predict_proba(m["home_team"], m["away_team"])
        elo.update(m["home_team"], m["away_team"], m["home_score"], m["away_score"])
        elo_predictions.append((elo_probs, outcome))

        poisson_probs = poisson.predict_proba(m["home_team"], m["away_team"])
        poisson_predictions.append((poisson_probs, outcome))

        mkt_probs = market_probs(m)
        if mkt_probs is not None:
            market_predictions.append((mkt_probs, outcome))

        ensemble_predictions.append((blend_probs(elo_probs, poisson_probs, mkt_probs, league=league), outcome))

    evaluate("Elo", elo_predictions)
    evaluate("Poisson", poisson_predictions)
    evaluate("Market", market_predictions)
    evaluate("Ensemble", ensemble_predictions)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", nargs="+", required=True)
    args = parser.parse_args()

    conn = get_connection()
    for league in args.league:
        backtest_league(conn, league)


if __name__ == "__main__":
    main()
