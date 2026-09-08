"""Does the rating engine actually recover skill?

The synthetic dataset knows every debater's true latent skill, so we can check
the thing that matters: are the ratings measuring skill, or just producing
plausible-looking numbers? Ordering (Spearman) matters more than absolute
agreement, since a rating system only has to rank correctly.

Run tests/../scripts/synth.py and scripts/build.py first, then:
    python tests/test_recovery.py
"""

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []


def check(name, got, floor):
    ok = got >= floor
    print("%-52s %-6s %.4f (need >= %.2f)" % (name, "ok" if ok else "FAIL", got, floor))
    if not ok:
        FAILED.append(name)


def check_true(name, cond, detail=""):
    print("%-52s %s %s" % (name, "ok" if cond else "FAIL", detail))
    if not cond:
        FAILED.append(name)


def spearman(xs, ys):
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        out = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


truth_path = os.path.join(ROOT, "data", "processed", "_synthetic_truth.json")
ratings_path = os.path.join(ROOT, "docs", "data", "ratings.json")
meta_path = os.path.join(ROOT, "docs", "data", "meta.json")

if not os.path.exists(truth_path):
    print("no synthetic truth file; run scripts/synth.py then scripts/build.py")
    sys.exit(0)

with open(truth_path, encoding="utf-8") as fh:
    truth = json.load(fh)
with open(ratings_path, encoding="utf-8") as fh:
    rows = json.load(fh)
with open(meta_path, encoding="utf-8") as fh:
    meta = json.load(fh)

paired = [(truth[r["id"]], r["rating"], r) for r in rows if r["id"] in truth]
print("%d debaters with known true skill\n" % len(paired))

# --- Ordering ----------------------------------------------------------------
rho_all = spearman([p[0] for p in paired], [p[1] for p in paired])
check("rank correlation with true skill (all)", rho_all, 0.70)

experienced = [p for p in paired if p[2]["rounds"] >= 40]
rho_exp = spearman([p[0] for p in experienced], [p[1] for p in experienced])
check("rank correlation, debaters with 40+ rounds", rho_exp, 0.80)
check_true("more rounds -> better ordering", rho_exp >= rho_all - 0.02,
           "(%.3f vs %.3f)" % (rho_exp, rho_all))

# --- The top of the board should really be the top of the board ---------------
by_rating = sorted(paired, key=lambda p: -p[1])[:25]
by_truth = set(id(p) for p in sorted(paired, key=lambda p: -p[0])[:50])
overlap = sum(1 for p in by_rating if id(p) in by_truth)
check("top-25 rated are inside the true top 50", overlap / 25.0, 0.72)

# --- RD must mean something ---------------------------------------------------
# Elo is an interval scale: the pool is pinned at a mean of 1500 while true skill
# is not, and shrinkage toward the prior deliberately compresses the spread. Both
# make raw |truth - rating| a measure of scale rather than of accuracy, so
# compare standardised residuals, which are invariant to both.
def zscores(vals):
    n = len(vals)
    mean = sum(vals) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / n) or 1.0
    return [(v - mean) / sd for v in vals]


zt = zscores([p[0] for p in paired])
zr = zscores([p[1] for p in paired])
resid = sorted(zip([p[2]["rd"] for p in paired], (abs(a - b) for a, b in zip(zt, zr))))
q = len(resid) // 4
low = sum(r for _, r in resid[:q]) / q
high = sum(r for _, r in resid[-q:]) / q
check_true("low-RD ratings are more accurate than high-RD ones", low < high,
           "(standardised residual %.3f vs %.3f)" % (low, high))

# The scale should not be badly compressed: that was the symptom of attributing
# team results to individuals without the 1/n slope.
sd_t = math.sqrt(sum((p[0] - sum(x[0] for x in paired) / len(paired)) ** 2
                     for p in paired) / len(paired))
sd_r = math.sqrt(sum((p[1] - sum(x[1] for x in paired) / len(paired)) ** 2
                     for p in paired) / len(paired))
check("rating spread retains most of the true spread", sd_r / sd_t, 0.70)

# --- Calibration --------------------------------------------------------------
bins = meta["metrics"]["calibration"]
worst = max(abs(b["predicted"] - b["actual"]) for b in bins if b["n"] >= 50)
check_true("calibration: no bucket off by more than 10pp", worst <= 0.10,
           "(worst bucket off by %.3f)" % worst)

acc = meta["metrics"]["all"]["accuracy"]
check("walk-forward accuracy beats a coin flip", acc, 0.58)
check_true("brier beats the always-50% baseline",
           meta["metrics"]["all"]["brier"] < 0.25,
           "(%.4f vs 0.2500)" % meta["metrics"]["all"]["brier"])

# --- Side bias should be recovered (synth injects +18 Elo of aff advantage) ----
detected = meta["side_bias"]["elo_offset"]
check_true("aff side bias detected with the right sign", detected > 0,
           "(detected %+.0f Elo, injected +18)" % detected)

print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all recovery tests passed")
