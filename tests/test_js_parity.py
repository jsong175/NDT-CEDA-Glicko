"""Check the dashboard's JavaScript maths against the Python engine.

The head-to-head calculator reimplements the Glicko team model in the browser.
If the two drift apart, the calculator quietly contradicts the rankings sitting
next to it, and nothing else in the test suite would catch it.

This compares Python's output against numbers captured from the real app.js
running in a browser. To regenerate the expected values after changing the
maths, see tests/js_parity_cases.json and the recipe in this file's footer.

Run: python tests/test_js_parity.py
"""

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from glicko2 import SCALE, Rating, combine_team, g, win_probability  # noqa: E402

CASES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "js_parity_cases.json")
FAILED = []


def check(name, got, want, tol):
    ok = abs(got - want) <= tol
    print("%-56s %-6s js=%.8f py=%.8f" % (name, "ok" if ok else "FAIL", want, got))
    if not ok:
        FAILED.append(name)


def side_adjusted(team_a, team_b, offset_elo):
    """Mirror of app.js winProb(A, B, advantageMu)."""
    mu_a, phi_a = combine_team(team_a)
    mu_b, phi_b = combine_team(team_b)
    spread = math.sqrt(phi_a ** 2 + phi_b ** 2)
    return 1.0 / (1.0 + math.exp(-g(spread) * (mu_a - mu_b + offset_elo / SCALE)))


with open(CASES_FILE, encoding="utf-8") as fh:
    cases = json.load(fh)

for i, case in enumerate(cases):
    a = [Rating(r, d) for r, d in case["a"]]
    b = [Rating(r, d) for r, d in case["b"]]

    mu_a, _ = combine_team(a)
    mu_b, _ = combine_team(b)
    check("case %d team A rating" % i, mu_a * SCALE + 1500, case["team_a"], 1e-3)
    check("case %d team B rating" % i, mu_b * SCALE + 1500, case["team_b"], 1e-3)
    check("case %d win probability" % i, win_probability(a, b), case["p"], 1e-7)
    check("case %d win probability, side bias" % i,
          side_adjusted(a, b, 11.0), case["p_side"], 1e-7)

print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    print("\nThe browser and Python disagree. Fix whichever is wrong, then "
          "regenerate the captured values (see below).")
    sys.exit(1)
print("all %d JS/Python parity checks passed" % (len(cases) * 4))

# Regenerating the captured values:
#   1. python -m http.server 8899 --directory docs
#   2. load docs/_parity.html in a browser (it calls window.__glicko directly)
#   3. paste its output into tests/js_parity_cases.json
