"""Blend the Elo, Poisson, and (when available) market-implied probabilities
into a single prediction.

Weights are a fixed heuristic, not fit by optimization: the backtests run
during development consistently ranked Market >= Poisson ~= Elo across all
four leagues, so the market (when we have odds for a fixture) gets the
largest share, Poisson next, Elo least. This is a deliberate simplification
for a personal project's scale -- see model/backtest.py's "Ensemble" row for
how it actually performs before tuning further.
"""

WEIGHTS_WITH_MARKET = {"market": 0.5, "poisson": 0.3, "elo": 0.2}
WEIGHTS_NO_MARKET = {"poisson": 0.6, "elo": 0.4}

Probs = tuple[float, float, float]


def blend_probs(elo: Probs, poisson: Probs, market: Probs | None = None) -> Probs:
    weights = WEIGHTS_WITH_MARKET if market is not None else WEIGHTS_NO_MARKET

    components = {"elo": elo, "poisson": poisson}
    if market is not None:
        components["market"] = market

    blended = [0.0, 0.0, 0.0]
    for name, probs in components.items():
        w = weights[name]
        for i in range(3):
            blended[i] += w * probs[i]

    total = sum(blended)
    return blended[0] / total, blended[1] / total, blended[2] / total
