"""Dixon-Coles Poisson goal model: each team gets an attack and defense
strength, fit by maximum likelihood over historical scorelines, plus:

  - a low-score correlation correction (the "rho" term from Dixon & Coles
    1997) that fixes the well-known bias of a plain product-of-Poissons
    model, which underrates 0-0/1-1 draws and overrates 1-0/0-1 wins;
  - an exponential time-decay weight so recent matches count for more than
    old ones when fitting team strengths (form drifts season to season --
    an unweighted fit blends a team's 2020 level with its 2026 level).

Expected goals:

    lambda_home = exp(home_adv + attack[home] - defense[away])
    lambda_away = exp(attack[away] - defense[home])

Attack/defense are only identifiable up to a shared additive shift (adding c
to every attack and defense leaves both lambdas unchanged), so the fit adds a
small L2 penalty pulling them toward 0 -- a standard, simple fix.
"""
from dataclasses import dataclass, field
from datetime import date as date_cls

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson as poisson_dist

L2_PENALTY = 0.01
MAX_GOALS = 8

# Dixon-Coles decay rate: weight = exp(-XI * days_before_most_recent_match).
# 0.0018/day gives a ~1-year half-life, in line with the value used in the
# original paper and most public implementations.
XI = 0.0018

# rho is bounded to keep every tau() value positive for realistic goal
# rates (lambda*mu rarely exceeds ~6-8), so log(tau) never blows up.
RHO_BOUNDS = (-0.3, 0.3)


def _tau(x: int, y: int, lam_home: float, lam_away: float, rho: float) -> float:
    """Dixon-Coles low-score correction factor; 1.0 outside {0,1}x{0,1}."""
    if x == 0 and y == 0:
        return 1 - lam_home * lam_away * rho
    if x == 0 and y == 1:
        return 1 + lam_home * rho
    if x == 1 and y == 0:
        return 1 + lam_away * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def _time_weights(dates: list[str]) -> np.ndarray:
    parsed = [date_cls.fromisoformat(d) for d in dates]
    latest = max(parsed)
    days_before = np.array([(latest - d).days for d in parsed], dtype=float)
    return np.exp(-XI * days_before)


@dataclass
class PoissonModel:
    teams: list = field(default_factory=list)
    attack: dict = field(default_factory=dict)
    defense: dict = field(default_factory=dict)
    home_adv: float = 0.2
    rho: float = 0.0

    def fit(self, matches: list[dict]) -> None:
        """matches: list of {home_team, away_team, home_score, away_score, date}."""
        self.teams = sorted({m["home_team"] for m in matches} | {m["away_team"] for m in matches})
        idx = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)

        home_i = np.array([idx[m["home_team"]] for m in matches])
        away_i = np.array([idx[m["away_team"]] for m in matches])
        home_g = np.array([m["home_score"] for m in matches], dtype=float)
        away_g = np.array([m["away_score"] for m in matches], dtype=float)
        weights = _time_weights([m["date"] for m in matches])

        low_score_mask = (home_g <= 1) & (away_g <= 1)

        def unpack(params):
            attack = params[:n]
            defense = params[n:2 * n]
            home_adv = params[2 * n]
            rho = params[2 * n + 1]
            return attack, defense, home_adv, rho

        def neg_log_likelihood(params):
            attack, defense, home_adv, rho = unpack(params)
            lam_home = np.exp(home_adv + attack[home_i] - defense[away_i])
            lam_away = np.exp(attack[away_i] - defense[home_i])

            base_ll = (
                -lam_home + home_g * np.log(lam_home)
                - lam_away + away_g * np.log(lam_away)
            )

            tau = np.ones_like(lam_home)
            idxs = np.nonzero(low_score_mask)[0]
            for i in idxs:
                tau[i] = _tau(int(home_g[i]), int(away_g[i]), lam_home[i], lam_away[i], rho)
            tau = np.clip(tau, 1e-6, None)

            weighted_ll = weights * (base_ll + np.log(tau))
            penalty = L2_PENALTY * (np.sum(attack ** 2) + np.sum(defense ** 2))
            return -weighted_ll.sum() + penalty

        x0 = np.zeros(2 * n + 2)
        x0[2 * n] = 0.2  # initial home_adv guess
        x0[2 * n + 1] = -0.05  # initial rho guess (Dixon-Coles found small negative values)
        bounds = [(None, None)] * (2 * n + 1) + [RHO_BOUNDS]
        result = minimize(neg_log_likelihood, x0, method="L-BFGS-B", bounds=bounds)

        attack, defense, home_adv, rho = unpack(result.x)
        self.attack = dict(zip(self.teams, attack))
        self.defense = dict(zip(self.teams, defense))
        self.home_adv = float(home_adv)
        self.rho = float(rho)

    def expected_goals(self, home_team: str, away_team: str) -> tuple[float, float]:
        a_home = self.attack.get(home_team, 0.0)
        d_home = self.defense.get(home_team, 0.0)
        a_away = self.attack.get(away_team, 0.0)
        d_away = self.defense.get(away_team, 0.0)
        lam_home = float(np.exp(self.home_adv + a_home - d_away))
        lam_away = float(np.exp(a_away - d_home))
        return lam_home, lam_away

    def predict_proba(self, home_team: str, away_team: str) -> tuple[float, float, float]:
        lam_home, lam_away = self.expected_goals(home_team, away_team)
        goals = np.arange(0, MAX_GOALS + 1)
        p_home_goals = poisson_dist.pmf(goals, lam_home)
        p_away_goals = poisson_dist.pmf(goals, lam_away)
        score_matrix = np.outer(p_home_goals, p_away_goals)  # [i, j] = P(home=i, away=j)

        for x in (0, 1):
            for y in (0, 1):
                score_matrix[x, y] *= _tau(x, y, lam_home, lam_away, self.rho)
        score_matrix = np.clip(score_matrix, 0, None)
        score_matrix /= score_matrix.sum()

        p_home_win = float(np.tril(score_matrix, k=-1).sum())
        p_draw = float(np.trace(score_matrix))
        p_away_win = float(np.triu(score_matrix, k=1).sum())

        return p_home_win, p_draw, p_away_win
