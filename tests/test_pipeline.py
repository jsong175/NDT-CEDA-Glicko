"""End-to-end checks on the built dashboard data.

Unlike test_recovery.py these do not need the synthetic truth file, so they run
against real scraped data too, and they are what CI relies on to catch a build
that silently produced nonsense.

Run: python tests/test_pipeline.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")
FAILED = []


def check(name, cond, detail=""):
    print("%-58s %s %s" % (name, "ok" if cond else "FAIL", detail))
    if not cond:
        FAILED.append(name)


for f in ("meta.json", "ratings.json"):
    if not os.path.exists(os.path.join(DATA, f)):
        print("docs/data/%s missing -- run scripts/build.py first" % f)
        sys.exit(1)

with open(os.path.join(DATA, "meta.json"), encoding="utf-8") as fh:
    meta = json.load(fh)
with open(os.path.join(DATA, "ratings.json"), encoding="utf-8") as fh:
    rows = json.load(fh)

print("%d debaters, %d rated rounds, %d tournaments\n"
      % (len(rows), meta["counts"]["debates"], meta["counts"]["tournaments"]))

# --- schema the dashboard depends on -----------------------------------------
need_meta = ["generated", "synthetic", "seasons", "counts", "config",
             "side_bias", "metrics", "tournaments", "defaults"]
check("meta.json carries every key app.js reads",
      all(k in meta for k in need_meta),
      str([k for k in need_meta if k not in meta]) if any(k not in meta for k in need_meta) else "")

need_row = ["id", "name", "school", "schools", "seasons", "rating", "rd", "vol",
            "floor", "peak", "w", "l", "rounds", "winpct", "tourns", "last",
            "provisional", "spark", "partners", "by_season"]
missing = set()
for r in rows:
    missing |= {k for k in need_row if k not in r}
check("every rating row carries every key app.js reads", not missing, str(sorted(missing)))

check("ratings.json is non-empty", len(rows) > 0)
check("at least one tournament was rated", meta["counts"]["tournaments"] > 0)

# --- internal consistency -----------------------------------------------------
bad = [r["id"] for r in rows if r["w"] + r["l"] != r["rounds"]]
check("wins + losses == rounds for everyone", not bad, str(bad[:3]))

bad = [r["id"] for r in rows if not (0 <= r["winpct"] <= 1)]
check("win percentages are in [0, 1]", not bad, str(bad[:3]))

bad = [r["id"] for r in rows if r["rd"] <= 0 or r["rd"] > meta["defaults"]["rd"] + 1]
check("RD is positive and never exceeds the ceiling", not bad, str(bad[:3]))

bad = [r["id"] for r in rows if r["peak"] < r["rating"] - 0.05]
check("peak rating is never below current rating", not bad, str(bad[:3]))

bad = [r["id"] for r in rows if r["rounds"] == 0]
check("nobody is rated with zero rounds", not bad, str(bad[:3]))

bad = [r["id"] for r in rows if len(r["spark"]) != r["tourns"]]
check("sparkline has one point per tournament", not bad, str(bad[:3]))

bad = [r["id"] for r in rows
       if [p[0] for p in r["spark"]] != sorted(p[0] for p in r["spark"])]
check("rating history is in date order", not bad, str(bad[:3]))

bad = [r["id"] for r in rows
       if sum(s["rounds"] for s in r["by_season"].values()) != r["rounds"]]
check("per-season rounds sum to the career total", not bad, str(bad[:3]))

# Ranks must be dense, ordered, and only on non-provisional debaters.
ranked = sorted([r for r in rows if "rank" in r], key=lambda r: r["rank"])
check("ranks are 1..N with no gaps",
      [r["rank"] for r in ranked] == list(range(1, len(ranked) + 1)))
check("no provisional debater is ranked", not any(r["provisional"] for r in ranked))
check("ranked ratings are in descending order",
      all(ranked[i]["rating"] >= ranked[i + 1]["rating"] - 1e-6
          for i in range(len(ranked) - 1)))

ids = [r["id"] for r in rows]
check("debater ids are unique", len(ids) == len(set(ids)))

# --- metrics ------------------------------------------------------------------
m = meta["metrics"]["all"]
check("predictions were actually scored", m and m["n"] == meta["counts"]["debates"],
      "%s vs %s" % (m["n"] if m else None, meta["counts"]["debates"]))
check("accuracy beats a coin flip", m["accuracy"] > 0.5, "%.3f" % m["accuracy"])
check("brier beats an uninformative 0.5 forecast", m["brier"] < 0.25, "%.4f" % m["brier"])
check("log loss beats an uninformative forecast", m["log_loss"] < 0.6931,
      "%.4f" % m["log_loss"])
check("calibration bins cover the probability range",
      len(meta["metrics"]["calibration"]) >= 4)

sb = meta["side_bias"]
check("aff win rate is plausible", 0.35 <= sb["aff_win_rate"] <= 0.65,
      "%.3f" % sb["aff_win_rate"])

# --- the provisional gate -----------------------------------------------------
# provisional_rd once sat below season_rd_floor. Because every rating is reopened
# to at least the floor when a season turns over, that flagged the whole pool as
# provisional on day one of each season and kept the best debaters -- who are
# rated on plenty of rounds -- off the default board all year.
cfg = meta.get("config", {})
check("provisional_rd clears the season RD floor",
      cfg.get("provisional_rd", 0) > cfg.get("season_rd_floor", 0),
      "%.0f vs %.0f" % (cfg.get("provisional_rd", 0), cfg.get("season_rd_floor", 0)))

ranked = [r for r in rows if not r["provisional"]]
check("the RD gate is not stricter than the rounds gate",
      all(r["provisional"] for r in rows if r["rounds"] < cfg["min_rounds_ranked"])
      and len(ranked) > 0)
thin = [r for r in rows if r["rounds"] >= 4 * cfg["min_rounds_ranked"] and r["provisional"]]
check("nobody with 4x the minimum sample is still provisional",
      not thin, "%d such: %s" % (len(thin), ", ".join(x["name"] for x in thin[:3])))
check("a majority of the pool is rankable",
      len(ranked) > len(rows) / 3, "%d of %d" % (len(ranked), len(rows)))

try:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from ratings import check_config
    bad = dict(cfg); bad["provisional_rd"] = bad["season_rd_floor"]
    try:
        check_config(bad)
        raised = False
    except ValueError:
        raised = True
    check("load_config rejects a cutoff under the floor", raised)
except ImportError as exc:  # pragma: no cover
    check("load_config rejects a cutoff under the floor", False, str(exc))


# --- cache hygiene ------------------------------------------------------------
# Tabroom serves its rate-limit notice as HTTP 200 with an ordinary-looking page,
# and the text only appears near the very END of the body. Cached, it is
# indistinguishable from the real page on every later run.
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import tabroom_client as tc  # noqa: E402

tail = "<html>" + ("x" * 22000) + ("<h4>Your requests have hit our rate limit; you "
                                   "may not access this page for another hour.</h4></html>")
check("rate-limit notice is caught at the tail of a page", tc._is_rate_limited(tail))
check("an ordinary page is not mistaken for one",
      not tc._is_rate_limited("<html>" + "x" * 30000 + "</html>"))

import glob  # noqa: E402
import gzip  # noqa: E402
poisoned = []
for f in glob.glob(os.path.join(tc.CACHE_DIR, "*", "*.html.gz")):
    try:
        with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
            if tc._is_rate_limited(fh.read()):
                poisoned.append(f)
    except OSError:
        pass
check("no rate-limit pages sitting in the cache", not poisoned,
      "%d found" % len(poisoned))


# --- person files -------------------------------------------------------------
person_dir = os.path.join(DATA, "person")
check("a detail file exists for every debater",
      os.path.isdir(person_dir)
      and all(os.path.exists(os.path.join(person_dir, r["id"] + ".json")) for r in rows))

if rows and os.path.isdir(person_dir):
    sample = rows[0]
    with open(os.path.join(person_dir, sample["id"] + ".json"), encoding="utf-8") as fh:
        person = json.load(fh)
    check("detail file round count matches the summary",
          len(person["rounds"]) == sample["rounds"],
          "%d vs %d" % (len(person["rounds"]), sample["rounds"]))
    check("detail file win count matches the summary",
          sum(1 for x in person["rounds"] if x["won"]) == sample["w"])
    bad = [x for x in person["rounds"] if not (0 <= x["p_win"] <= 1)]
    check("stored win probabilities are in [0, 1]", not bad)
    ids = {r["id"] for r in rows}
    check("every round names its opponents",
          all(len(x["opponents"]) == 2 for x in person["rounds"]))
    check("opponent ids all resolve to a rated debater",
          all(o in ids for x in person["rounds"] for o in x["opponents"]))

print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all pipeline tests passed")
