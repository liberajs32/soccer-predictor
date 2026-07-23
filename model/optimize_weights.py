"""Grid-search the fixed ensemble weights in model/ensemble.py.

Uses the exact same train/test split and per-match Elo/Poisson/market
probabilities as model/backtest.py, but instead of evaluating one fixed
weight combo, sweeps the whole weight simplex (step 0.05) pooled across all
requested leagues and reports the combo that minimizes average log_loss --
log_loss is the right thing to optimize (not accuracy) because it's what the
Elo/Poisson fits themselves are implicitly minimizing, and it rewards
well-calibrated probabilities rather than just the argmax.

Usage:
    python model/optimize_weights.py --league EPL K1 K2 BL1
"""
import argparse
import itertools
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from elo import EloModel, outcome_label
from poisson import PoissonModel

from db import get_connection
from backtest import TRAIN_FRACTION, EPS, load_decided_matches, market_probs, log_loss, accuracy_hit

STEP = 0.05


def collect_match_probs(conn, leagues: list[str]) -> tuple[list, list]:
    """Returns (with_market, no_market), each a list of (elo_probs, poisson_probs,
    market_probs_or_None, outcome) tuples pooled across all given leagues."""
    with_market, no_market = [], []
    for league in leagues:
        matches = load_decided_matches(conn, league)
        if len(matches) < 20:
            print(f"{league}: only {len(matches)} decided matches, skipping")
            continue
        split = int(len(matches) * TRAIN_FRACTION)
        train, test = matches[:split], matches[split:]

        elo = EloModel()
        train_pairs = []
        for m in train:
            gap = elo.rating_gap(m["home_team"], m["away_team"])
            train_pairs.append((gap, outcome_label(m["home_score"], m["away_score"])))
            elo.update(m["home_team"], m["away_team"], m["home_score"], m["away_score"])
        elo.fit_outcome_mapping(train_pairs)

        poisson = PoissonModel()
        poisson.fit(train)

        for m in test:
            outcome = outcome_label(m["home_score"], m["away_score"])
            elo_probs = elo.predict_proba(m["home_team"], m["away_team"])
            elo.update(m["home_team"], m["away_team"], m["home_score"], m["away_score"])
            poisson_probs = poisson.predict_proba(m["home_team"], m["away_team"])
            mkt = market_probs(m)
            row = (elo_probs, poisson_probs, mkt, outcome)
            (with_market if mkt is not None else no_market).append(row)
    return with_market, no_market


def _weight_grid(names: tuple[str, ...]):
    """All (name -> weight) dicts on the simplex with the given step, i.e.
    weights are non-negative multiples of STEP that sum to 1."""
    n = len(names)
    ticks = round(1.0 / STEP)
    for combo in itertools.product(range(ticks + 1), repeat=n - 1):
        if sum(combo) > ticks:
            continue
        last = ticks - sum(combo)
        values = [c * STEP for c in combo] + [last * STEP]
        yield dict(zip(names, values))


def blend_with(weights: dict, elo_probs, poisson_probs, market_probs_):
    components = {"elo": elo_probs, "poisson": poisson_probs}
    if market_probs_ is not None:
        components["market"] = market_probs_
    blended = [0.0, 0.0, 0.0]
    for name, probs in components.items():
        w = weights[name]
        for i in range(3):
            blended[i] += w * probs[i]
    total = sum(blended)
    if total < EPS:
        return 1 / 3, 1 / 3, 1 / 3
    return blended[0] / total, blended[1] / total, blended[2] / total


def search(rows: list, names: tuple[str, ...], label: str) -> dict | None:
    """Reports the best weight combo under two different objectives, since
    they can disagree: log_loss rewards well-calibrated probabilities,
    accuracy rewards picking the right argmax outcome, which is what
    actually matters when the output is used as a single 1X2 pick."""
    if not rows:
        print(f"{label}: no data")
        return None

    best_by_loss, best_loss = None, math.inf
    best_by_acc, best_acc = None, -1.0
    for weights in _weight_grid(names):
        total_loss, hits = 0.0, 0
        for elo_probs, poisson_probs, mkt, outcome in rows:
            probs = blend_with(weights, elo_probs, poisson_probs, mkt)
            total_loss += log_loss(probs, outcome)
            hits += accuracy_hit(probs, outcome)
        avg_loss = total_loss / len(rows)
        acc = hits / len(rows)
        if avg_loss < best_loss:
            best_loss, best_by_loss = avg_loss, weights
        if acc > best_acc:
            best_acc, best_by_acc = acc, weights

    print(f"{label}: n={len(rows)}")
    print(f"    best log_loss : {best_by_loss}  log_loss={best_loss:.4f}")
    print(f"    best accuracy : {best_by_acc}  accuracy={best_acc:.3f}")
    return best_by_loss


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", nargs="+", required=True)
    args = parser.parse_args()

    conn = get_connection()

    for league in args.league:
        wm, nm = collect_match_probs(conn, [league])
        print(f"\n{league}:")
        search(wm, ("market", "poisson", "elo"), "  WITH market")
        search(nm, ("poisson", "elo"), "  NO market")

    print(f"\nPooled across {args.league}:")
    with_market, no_market = collect_match_probs(conn, args.league)
    search(with_market, ("market", "poisson", "elo"), "WITH market")
    search(no_market, ("poisson", "elo"), "NO market")


if __name__ == "__main__":
    main()
