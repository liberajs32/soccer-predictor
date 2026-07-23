"""Blend the Elo, Poisson, and (when available) market-implied probabilities
into a single prediction.

Weights are grid-searched per league (model/optimize_weights.py), optimizing
backtest *accuracy* rather than log_loss -- the app outputs a single 1X2
pick, so what matters is how often the argmax is right, not how well the
full distribution is calibrated. EPL/K1/BL1 markets turned out efficient
enough that our own Elo/Poisson barely move the needle over market alone;
K2 -- much lower betting volume/liquidity -- is the one league where
blending in Poisson meaningfully helps. Re-run optimize_weights.py as more
results accumulate (data as of 2026-07-23, ~2000 decided matches pooled) and
update these.
"""

WEIGHTS_WITH_MARKET = {
    "EPL": {"market": 0.90, "poisson": 0.05, "elo": 0.05},
    "K1": {"market": 0.95, "poisson": 0.00, "elo": 0.05},
    "K2": {"market": 0.55, "poisson": 0.35, "elo": 0.10},
    "BL1": {"market": 0.95, "poisson": 0.05, "elo": 0.00},
}
DEFAULT_WEIGHTS_WITH_MARKET = {"market": 0.90, "poisson": 0.05, "elo": 0.05}
WEIGHTS_NO_MARKET = {"poisson": 0.6, "elo": 0.4}

Probs = tuple[float, float, float]


def blend_probs(elo: Probs, poisson: Probs, market: Probs | None = None, league: str | None = None) -> Probs:
    if market is not None:
        weights = WEIGHTS_WITH_MARKET.get(league, DEFAULT_WEIGHTS_WITH_MARKET)
    else:
        weights = WEIGHTS_NO_MARKET

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
