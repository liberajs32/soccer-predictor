"""Elo-rating baseline: maintains per-team ratings from match results, and
fits a small 3-class logistic model that maps the home/away rating gap to
1X2 probabilities.

Rating update is the standard chess-style Elo formula with a home-advantage
offset. The rating gap alone doesn't say how likely a *draw* is (Elo was
designed for win/lose games), so a compact multinomial logistic regression
(away team as the reference class) is fit on historical (rating_gap, outcome)
pairs via plain gradient descent -- no numpy/sklearn needed at this scale
(a few hundred matches, 4 parameters).
"""
import math
from dataclasses import dataclass, field

K_FACTOR = 20.0
HOME_ADVANTAGE = 65.0
INITIAL_RATING = 1500.0


@dataclass
class EloModel:
    ratings: dict = field(default_factory=dict)
    # softmax params: z_home = a_h + b_h*gap, z_draw = a_d + b_d*gap, z_away = 0
    a_h: float = 0.4   # home advantage shows up here too (positive baseline edge)
    b_h: float = 0.0
    a_d: float = -0.2
    b_d: float = 0.0

    def rating(self, team: str) -> float:
        return self.ratings.get(team, INITIAL_RATING)

    def rating_gap(self, home_team: str, away_team: str) -> float:
        return self.rating(home_team) + HOME_ADVANTAGE - self.rating(away_team)

    def update(self, home_team: str, away_team: str, home_score: int, away_score: int) -> None:
        r_home, r_away = self.rating(home_team), self.rating(away_team)
        expected_home = 1.0 / (1.0 + 10 ** (-(r_home + HOME_ADVANTAGE - r_away) / 400.0))
        if home_score > away_score:
            actual_home = 1.0
        elif home_score == away_score:
            actual_home = 0.5
        else:
            actual_home = 0.0
        self.ratings[home_team] = r_home + K_FACTOR * (actual_home - expected_home)
        self.ratings[away_team] = r_away - K_FACTOR * (actual_home - expected_home)

    def predict_proba(self, home_team: str, away_team: str) -> tuple[float, float, float]:
        gap = self.rating_gap(home_team, away_team)
        return self._softmax(gap)

    def _softmax(self, gap: float) -> tuple[float, float, float]:
        # Scale the raw Elo gap (typically -400..+400) down to O(1) before
        # applying the linear logit -- feeding gradient descent a raw gap
        # alongside O(1) intercepts is badly ill-conditioned (a single shared
        # learning rate is either too big for the slope or too small for the
        # intercept) and was observed to converge to a sign-flipped slope.
        scaled_gap = gap / 400.0
        z_home = self.a_h + self.b_h * scaled_gap
        z_draw = self.a_d + self.b_d * scaled_gap
        m = max(z_home, z_draw, 0.0)  # numerical stability
        e_home, e_draw, e_away = math.exp(z_home - m), math.exp(z_draw - m), math.exp(0.0 - m)
        total = e_home + e_draw + e_away
        return e_home / total, e_draw / total, e_away / total

    def fit_outcome_mapping(self, gaps_outcomes: list[tuple[float, str]], lr: float = 0.1, epochs: int = 2000) -> None:
        """gaps_outcomes: list of (rating_gap_before_match, outcome) where
        outcome is 'H'/'D'/'A'. Fits a_h,b_h,a_d,b_d by gradient descent on
        multinomial cross-entropy loss."""
        if not gaps_outcomes:
            return
        for _ in range(epochs):
            grad_a_h = grad_b_h = grad_a_d = grad_b_d = 0.0
            for gap, outcome in gaps_outcomes:
                scaled_gap = gap / 400.0
                p_home, p_draw, p_away = self._softmax(gap)
                y_home = 1.0 if outcome == "H" else 0.0
                y_draw = 1.0 if outcome == "D" else 0.0
                err_home = p_home - y_home
                err_draw = p_draw - y_draw
                grad_a_h += err_home
                grad_b_h += err_home * scaled_gap
                grad_a_d += err_draw
                grad_b_d += err_draw * scaled_gap
            n = len(gaps_outcomes)
            self.a_h -= lr * grad_a_h / n
            self.b_h -= lr * grad_b_h / n
            self.a_d -= lr * grad_a_d / n
            self.b_d -= lr * grad_b_d / n


def outcome_label(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "H"
    if home_score == away_score:
        return "D"
    return "A"
