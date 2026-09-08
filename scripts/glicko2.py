"""
Glicko-2 with a team -> individual extension, for 2v2 policy debate.

Glicko-2 (Glickman 2012, http://www.glicko.net/glicko/glicko2.pdf) rates a
single player from the games they played inside one "rating period". Debate is
played by two-person teams whose composition changes constantly, so we need to
attribute a team result back to the individuals.

The extension used here is the standard "team = mean of its members" model, the
same shape TrueSkill uses for team games:

    mu_T   = mean(mu_i for i in team)
    phi_T  = sqrt(sum(phi_i**2)) / n        # sd of the mean of n independents

A debate between team A and team B is then scored, for each player i in A, as a
single Glicko-2 game against one virtual opponent carrying (mu_B, phi_B). Two
consequences fall out of this and both are desirable:

  * A player's own phi_i still drives the size of their own update, so a
    high-uncertainty novice partnered with a settled star moves much further on
    the same result than the star does.
  * Beating a strong team lifts both partners; the credit is not split, because
    both debaters genuinely participated in every round.

Rating period = one tournament. Every round a debater takes at a tournament is
one game inside that period, which is the granularity Glickman recommends
(5-10 games per period).

Pure stdlib, no dependencies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

# Glicko-2 scale factor: 400/ln(10)
SCALE = 173.7178
DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06

# System constant tau constrains volatility change. Glickman suggests 0.3-1.2;
# smaller values suit results that are not very erratic. Debate outcomes are
# noisier than chess but far less noisy than, say, poker.
DEFAULT_TAU = 0.5

CONVERGENCE = 1e-6
MAX_ITER = 100


@dataclass(frozen=True)
class Rating:
    """A player's rating state on the human-readable (Glicko-1) scale."""

    rating: float = DEFAULT_RATING
    rd: float = DEFAULT_RD
    vol: float = DEFAULT_VOL

    @property
    def mu(self):
        return (self.rating - DEFAULT_RATING) / SCALE

    @property
    def phi(self):
        return self.rd / SCALE

    @classmethod
    def from_mu_phi(cls, mu, phi, vol):
        return cls(mu * SCALE + DEFAULT_RATING, phi * SCALE, vol)

    def conservative(self, k=2.0):
        """Rating minus k RDs: the "confident they are at least this good"
        number. Used for leaderboard ordering so a 3-0 newcomer sitting on an
        RD of 290 does not outrank a settled 40-round veteran."""
        return self.rating - k * self.rd


def g(phi):
    """Glicko-2 weighting for an opponent's rating deviation."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def expect(mu, mu_j, phi_j):
    """Expected score for a player at mu against an opponent at (mu_j, phi_j)."""
    return 1.0 / (1.0 + math.exp(-g(phi_j) * (mu - mu_j)))


def combine_team(ratings):
    """Collapse a team into a single virtual opponent (mu, phi), Glicko-2 units.

    The mean of n independent estimates has variance sum(phi_i^2)/n^2, hence the
    division by n rather than sqrt(n): a two-person team is a better-known
    quantity than either member alone, insofar as their errors are independent,
    which is the assumption we make here.
    """
    if not ratings:
        raise ValueError("cannot combine an empty team")
    n = len(ratings)
    mu = sum(r.mu for r in ratings) / n
    phi = math.sqrt(sum(r.phi ** 2 for r in ratings)) / n
    return mu, phi


def win_probability(team_a, team_b):
    """Probability that team A beats team B, accounting for both teams' RD.

    This is the predictive probability, not the point estimate: uncertainty on
    either side pulls the answer toward 50%, which is the honest thing to show
    someone asking about a matchup involving a debater with three rounds on
    record.
    """
    mu_a, phi_a = combine_team(team_a)
    mu_b, phi_b = combine_team(team_b)
    # Fold both teams' uncertainty into the g() term.
    spread = math.sqrt(phi_a ** 2 + phi_b ** 2)
    return 1.0 / (1.0 + math.exp(-g(spread) * (mu_a - mu_b)))


@dataclass
class Game:
    """One round from one player's point of view, inside a rating period.

    `slope` is what makes the team model work. A debate is decided by team
    strength, and a debater is only 1/n of their team, so their rating moves the
    outcome probability only 1/n as much as a singles player's would. Setting
    slope = 1/n linearises that correctly:

        E = sigmoid( g(opp_phi) * slope * (mu - opp_mu) )

    Leaving slope at 1.0 (the singles case) instead computes each debater's
    expectation from their own rating against the opponent *team*, which is
    inconsistent with how the result was actually produced -- a star carrying a
    weak partner would be punished for drawing at team level, and their partner
    rewarded. In testing against known latent skill that inconsistency
    compressed the whole rating scale by a factor of two.

    See build_team_games() for the caller that sets these up.
    """

    opp_mu: float
    opp_phi: float
    score: float          # 1.0 win, 0.0 loss; fractional allowed (ballot share)
    weight: float = 1.0   # >1 for elim panels, when ballot weighting is on
    slope: float = 1.0    # 1/n for an n-person team; 1.0 for singles


def build_team_games(team, opponents, score, weight=1.0, advantage=0.0):
    """Turn one team-vs-team round into one Game per member of `team`.

    The round was decided by team strength, mu_T = mean(mu_i). Holding the
    partners fixed, the team difference seen by member i is

        D = (mu_i - opp_mu_eff) / n
        opp_mu_eff = n * (mu_opp_team - advantage) - sum(partner mu)

    which is an exact rewrite, not an approximation: feeding `opp_mu_eff` with
    slope 1/n reproduces the team-level expectation while leaving mu_i as the
    only free variable. `advantage` is any edge this team had that is not skill
    (the aff side bias), in Glicko-2 units.

    Uncertainty on the other side of D comes from both the opponents and one's
    own partner, the latter scaled by 1/n because that is how much of the team
    mean the partner accounts for.
    """
    n = len(team)
    if n == 0 or not opponents:
        return []
    opp_mu, opp_phi = combine_team(opponents)
    games = []
    for i, _member in enumerate(team):
        partners = [team[j] for j in range(n) if j != i]
        partner_mu = sum(p.mu for p in partners)
        partner_var = sum(p.phi ** 2 for p in partners) / (n * n)
        games.append(Game(
            opp_mu=n * (opp_mu - advantage) - partner_mu,
            opp_phi=math.sqrt(opp_phi ** 2 + partner_var),
            score=score,
            weight=weight,
            slope=1.0 / n,
        ))
    return games


def _new_volatility(phi, v, delta, sigma, tau):
    """Illinois-variant regula falsi solve for sigma', per Glickman step 5."""
    a = math.log(sigma * sigma)
    phi2 = phi * phi
    delta2 = delta * delta

    def f(x):
        ex = math.exp(x)
        denom = phi2 + v + ex
        return (ex * (delta2 - phi2 - v - ex)) / (2.0 * denom * denom) - (x - a) / (tau * tau)

    a_side = a
    if delta2 > phi2 + v:
        b_side = math.log(delta2 - phi2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0 and k < MAX_ITER:
            k += 1
        b_side = a - k * tau

    f_a, f_b = f(a_side), f(b_side)
    it = 0
    while abs(b_side - a_side) > CONVERGENCE and it < MAX_ITER:
        c_side = a_side + (a_side - b_side) * f_a / (f_b - f_a)
        f_c = f(c_side)
        if f_c * f_b <= 0:
            a_side, f_a = b_side, f_b
        else:
            f_a /= 2.0
        b_side, f_b = c_side, f_c
        it += 1
    return math.exp(a_side / 2.0)


def rate_period(rating, games, tau=DEFAULT_TAU):
    """Advance one player through one rating period.

    With no games the rating is unchanged but RD grows by the volatility, which
    is what makes a debater who sat out the fall show up in January with a wide
    interval instead of a stale-but-confident number.
    """
    if not games:
        phi_star = math.sqrt(rating.phi ** 2 + rating.vol ** 2)
        return Rating.from_mu_phi(rating.mu, phi_star, rating.vol)

    mu, phi, sigma = rating.mu, rating.phi, rating.vol

    v_inv = 0.0
    delta_sum = 0.0
    for gm in games:
        gj = g(gm.opp_phi) * gm.slope
        e = 1.0 / (1.0 + math.exp(-gj * (mu - gm.opp_mu)))
        v_inv += gm.weight * gj * gj * e * (1.0 - e)
        delta_sum += gm.weight * gj * (gm.score - e)

    if v_inv <= 0.0:
        # Degenerate: every game was a certainty. Treat the period as no news.
        phi_star = math.sqrt(phi ** 2 + sigma ** 2)
        return Rating.from_mu_phi(mu, phi_star, sigma)

    v = 1.0 / v_inv
    delta = v * delta_sum

    sigma_p = _new_volatility(phi, v, delta, sigma, tau)
    phi_star = math.sqrt(phi ** 2 + sigma_p ** 2)
    phi_p = 1.0 / math.sqrt(1.0 / (phi_star ** 2) + v_inv)
    mu_p = mu + (phi_p ** 2) * delta_sum

    return Rating.from_mu_phi(mu_p, phi_p, sigma_p)


def decay_inactive(rating, periods=1.0, max_rd=DEFAULT_RD):
    """Widen RD for `periods` of inactivity without touching the rating."""
    phi = math.sqrt(rating.phi ** 2 + periods * rating.vol ** 2)
    return replace(rating, rd=min(phi * SCALE, max_rd))


def carry_over_season(rating, regression=0.25, rd_floor=150.0, max_rd=DEFAULT_RD):
    """Roll a rating from one season into the next.

    Two things happen over a summer: the topic and the field turn over, so we
    regress `regression` of the way back to 1500; and we know less in September
    than we did in April, so RD is bumped to at least `rd_floor`. Neither
    constant has a principled value -- both are calibrated against held-out
    prediction accuracy in evaluate.py.
    """
    new_rating = rating.rating + (DEFAULT_RATING - rating.rating) * regression
    return Rating(new_rating, min(max(rating.rd, rd_floor), max_rd), rating.vol)
