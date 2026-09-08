"""Correctness tests for the rating engine.

The first test is the worked example from Glickman's own Glicko-2 paper; if it
passes, the core update is right and everything else is our own layering on top.
Run with: python tests/test_glicko2.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from glicko2 import (  # noqa: E402
    Game,
    Rating,
    carry_over_season,
    combine_team,
    decay_inactive,
    rate_period,
    win_probability,
)

FAILED = []


def check(name, got, want, tol):
    ok = abs(got - want) <= tol
    print("%-58s %-12s got=%.6f want=%.6f" % (name, "ok" if ok else "FAIL", got, want))
    if not ok:
        FAILED.append(name)


def check_true(name, cond):
    print("%-58s %s" % (name, "ok" if cond else "FAIL"))
    if not cond:
        FAILED.append(name)


# --- Glickman's worked example (glicko2.pdf, section 'Example') ---------------
# Player 1500/200/0.06, tau=0.5, beats 1400/30, loses to 1550/100 and 1700/300.
player = Rating(1500, 200, 0.06)
opponents = [Rating(1400, 30), Rating(1550, 100), Rating(1700, 300)]
scores = [1.0, 0.0, 0.0]
games = [Game(o.mu, o.phi, s) for o, s in zip(opponents, scores)]
result = rate_period(player, games, tau=0.5)

check("glickman example: rating", result.rating, 1464.06, 0.01)
check("glickman example: RD", result.rd, 151.52, 0.01)
check("glickman example: volatility", result.vol, 0.05999, 0.0001)

# --- No games in a period: rating held, RD widens -----------------------------
idle = rate_period(Rating(1700, 80, 0.06), [], tau=0.5)
check("idle period: rating unchanged", idle.rating, 1700.0, 1e-9)
check_true("idle period: RD widened", idle.rd > 80.0)

# --- Team combination ---------------------------------------------------------
mu, phi = combine_team([Rating(1600, 100), Rating(1400, 100)])
check("team mu is the mean of members", mu * 173.7178 + 1500, 1500.0, 1e-6)
# sd of the mean of two independents with phi each = phi/sqrt(2) < phi
check_true("team phi tighter than a member phi", phi < Rating(1600, 100).phi)

# --- Win probability ----------------------------------------------------------
even = win_probability([Rating(1500, 50), Rating(1500, 50)],
                       [Rating(1500, 50), Rating(1500, 50)])
check("identical teams -> 50%", even, 0.5, 1e-9)

strong = win_probability([Rating(1900, 50), Rating(1900, 50)],
                         [Rating(1300, 50), Rating(1300, 50)])
check_true("600 pt edge -> strong favourite", strong > 0.90)

sym = win_probability([Rating(1800, 60), Rating(1500, 90)],
                      [Rating(1600, 70), Rating(1400, 80)])
inv = win_probability([Rating(1600, 70), Rating(1400, 80)],
                      [Rating(1800, 60), Rating(1500, 90)])
check("win probability is symmetric", sym + inv, 1.0, 1e-9)

# Uncertainty must pull the answer toward the coin flip.
confident = win_probability([Rating(1800, 40), Rating(1800, 40)],
                            [Rating(1500, 40), Rating(1500, 40)])
unsure = win_probability([Rating(1800, 330), Rating(1800, 330)],
                         [Rating(1500, 330), Rating(1500, 330)])
check_true("high RD shrinks the edge toward 50%", unsure < confident)

# --- Partner asymmetry: the uncertain debater should move further -------------
# A settled star and an unrated newcomer play the same four rounds together and
# win them all against 1500/80 opposition.
star, rookie = Rating(1900, 45, 0.06), Rating(1500, 350, 0.06)
opp_pair = [Rating(1500, 80), Rating(1500, 80)]
opp_mu, opp_phi = combine_team(opp_pair)
sweep = [Game(opp_mu, opp_phi, 1.0) for _ in range(4)]
star_after = rate_period(star, sweep)
rookie_after = rate_period(rookie, sweep)
check_true("rookie moves further than the star on the same sweep",
           abs(rookie_after.rating - rookie.rating) > abs(star_after.rating - star.rating))
# The rookie learns a lot from four rounds; the star learns almost nothing from
# beating teams it was supposed to beat, so its RD barely moves (and may even
# widen a point or two, since volatility inflation outweighs the information
# gained). Both are correct Glicko-2 behaviour.
check_true("rookie RD shrinks sharply", rookie_after.rd < rookie.rd - 100)
check_true("star RD stays put on expected wins", abs(star_after.rd - star.rd) < 5)

# --- Beating a stronger team must not lower your rating -----------------------
mid = Rating(1500, 100, 0.06)
up_mu, up_phi = combine_team([Rating(1900, 60), Rating(1900, 60)])
after_upset = rate_period(mid, [Game(up_mu, up_phi, 1.0)])
check_true("upset win raises rating", after_upset.rating > mid.rating)
down_mu, down_phi = combine_team([Rating(1100, 60), Rating(1100, 60)])
after_choke = rate_period(mid, [Game(down_mu, down_phi, 0.0)])
check_true("loss to a weak team lowers rating", after_choke.rating < mid.rating)

# --- Season carryover ---------------------------------------------------------
carried = carry_over_season(Rating(1900, 60, 0.06), regression=0.25, rd_floor=150)
check("carryover regresses 25% toward 1500", carried.rating, 1800.0, 1e-9)
check("carryover applies the RD floor", carried.rd, 150.0, 1e-9)
check_true("carryover never pushes RD past the max",
           carry_over_season(Rating(1500, 400, 0.06)).rd <= 350.0)

# --- Inactivity decay is monotone and capped ----------------------------------
d1 = decay_inactive(Rating(1700, 80, 0.06), periods=1)
d5 = decay_inactive(Rating(1700, 80, 0.06), periods=5)
check_true("more idle periods -> wider RD", d5.rd > d1.rd)
check_true("decay is capped at the default RD",
           decay_inactive(Rating(1700, 80, 0.9), periods=50).rd <= 350.0)

# --- Numerical sanity ---------------------------------------------------------
extreme = rate_period(Rating(2400, 30, 0.06),
                      [Game(*combine_team([Rating(900, 30), Rating(900, 30)]), 0.0)])
check_true("extreme mismatch stays finite",
           math.isfinite(extreme.rating) and math.isfinite(extreme.rd)
           and math.isfinite(extreme.vol))

print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all rating-engine tests passed")
