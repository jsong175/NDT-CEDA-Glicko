"""Build the dashboard's JSON from processed debates.

Reads data/processed/{tournaments,people}.json + debates.jsonl, runs the
Glicko-2 engine, and writes docs/data/ for the static site:

    meta.json          seasons, config, walk-forward accuracy, tournament list
    ratings.json       one row per debater: rating, RD, record, sparkline
    person/<id>.json   that debater's full round-by-round log

Usage:  python scripts/build.py [--season 2025-26] [--config config.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import glicko2 as gl  # noqa: E402
import ratings as rt  # noqa: E402

ROOT = rt.ROOT
PROC = rt.PROC
OUT = rt.OUT


def load_inputs(divisions):
    tourn_path = os.path.join(PROC, "tournaments.json")
    people_path = os.path.join(PROC, "people.json")
    debates_path = os.path.join(PROC, "debates.jsonl")

    for path in (tourn_path, debates_path):
        if not os.path.exists(path):
            raise SystemExit(
                "missing %s -- run scripts/scrape.py first (or scripts/synth.py "
                "to generate a sample dataset)" % os.path.relpath(path, ROOT))

    with open(tourn_path, "r", encoding="utf-8") as fh:
        tournaments = json.load(fh)
    people = {}
    if os.path.exists(people_path):
        with open(people_path, "r", encoding="utf-8") as fh:
            people = json.load(fh)

    debates = [d for d in rt.load_jsonl(debates_path)
               if d.get("division") in divisions]
    return tournaments, people, debates


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-rounds", type=int, default=None,
                    help="override config min_rounds_ranked")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = rt.load_config()
    if args.min_rounds is not None:
        cfg["min_rounds_ranked"] = args.min_rounds

    tournaments, people, debates = load_inputs(set(cfg["divisions"]))
    if not debates:
        raise SystemExit("no debates matched divisions %s" % cfg["divisions"])

    engine = rt.Engine(cfg)
    engine.run(debates, tournaments)

    tourn_by_id = {t["tourn_id"]: t for t in tournaments}
    rated_ids = {d["tourn_id"] for d in debates}
    used_tourns = sorted(
        (tourn_by_id[t] for t in rated_ids if t in tourn_by_id),
        key=lambda t: (t.get("start") or "", t["tourn_id"]))

    seasons = sorted({t.get("season") for t in used_tourns if t.get("season")})
    latest_date = max((t.get("start") or "" for t in used_tourns), default="")

    # --- ratings.json ---------------------------------------------------------
    rows = []
    for pid, rating in engine.ratings.items():
        rec = engine.record[pid]
        if rec["rounds"] == 0:
            continue
        info = people.get(pid, {})
        hist = engine.history[pid]
        partners = sorted(engine.partners[pid].items(), key=lambda kv: -kv[1])

        # Per-season snapshot: the rating as it stood after that season's last
        # tournament, paired with that season's own record.
        by_season = {}
        for season in seasons:
            srec = engine.season_record[pid].get(season)
            if not srec or not srec["rounds"]:
                continue
            marks = [h for h in hist if h["season"] == season]
            by_season[season] = {
                "rating": marks[-1]["rating"] if marks else None,
                "rd": marks[-1]["rd"] if marks else None,
                "w": srec["w"], "l": srec["l"], "rounds": srec["rounds"],
                "tourns": len(marks),
            }
        rows.append({
            "id": pid,
            "name": info.get("name") or pid.replace("-", " ").title(),
            "school": (info.get("schools") or [""])[-1],
            "schools": info.get("schools") or [],
            "seasons": info.get("seasons") or [],
            "rating": round(rating.rating, 1),
            "rd": round(rating.rd, 1),
            "vol": round(rating.vol, 5),
            "floor": round(rating.conservative(2.0), 1),
            "peak": round(engine.peak.get(pid, rating.rating), 1),
            "w": rec["w"],
            "l": rec["l"],
            "rounds": rec["rounds"],
            "winpct": round(rec["w"] / rec["rounds"], 4) if rec["rounds"] else 0.0,
            "tourns": len(hist),
            "last": hist[-1]["date"] if hist else None,
            "last_season": engine.last_season.get(pid),
            "provisional": rating.rd > cfg["provisional_rd"]
                           or rec["rounds"] < cfg["min_rounds_ranked"],
            # Season is carried on each point so the dashboard can slice the
            # sparkline by season without guessing from the date.
            "spark": [[h["date"], h["rating"], h["season"]] for h in hist],
            "partners": [{"id": p, "n": n} for p, n in partners[:6]],
            "by_season": by_season,
        })

    rows.sort(key=lambda r: -r["rating"])
    for i, row in enumerate(r for r in rows if not r["provisional"]):
        row["rank"] = i + 1

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "ratings.json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, separators=(",", ":"))

    # --- person/<id>.json -----------------------------------------------------
    person_dir = os.path.join(OUT, "person")
    if os.path.isdir(person_dir):
        shutil.rmtree(person_dir)
    os.makedirs(person_dir, exist_ok=True)
    name_of = {r["id"]: r["name"] for r in rows}
    order = {t["tourn_id"]: i for i, t in enumerate(used_tourns)}
    for pid in engine.ratings:
        if engine.record[pid]["rounds"] == 0:
            continue
        # Names, tournament names, and dates are deliberately not written here:
        # ratings.json and meta.json already carry them, and repeating them per
        # round nearly tripled the size of docs/ for data the client has.
        log = sorted(engine.log[pid], key=lambda r: order.get(r["tourn_id"], 0))
        with open(os.path.join(person_dir, "%s.json" % pid), "w", encoding="utf-8") as fh:
            json.dump({"id": pid, "name": name_of.get(pid, pid),
                       "history": engine.history[pid], "rounds": log},
                      fh, separators=(",", ":"))

    # --- meta.json ------------------------------------------------------------
    metrics = engine.metrics()
    synthetic = sum(1 for d in debates if d.get("synthetic"))
    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Loud on purpose: the dashboard shows a banner whenever any rated round
        # came from the simulator rather than from Tabroom.
        "synthetic": synthetic > 0,
        "synthetic_share": round(synthetic / len(debates), 4) if debates else 0,
        "seasons": seasons,
        "current_season": seasons[-1] if seasons else None,
        "latest_tournament_date": latest_date,
        "counts": {
            "debaters": len(rows),
            "ranked": sum(1 for r in rows if not r["provisional"]),
            "debates": len(debates),
            "tournaments": len(used_tourns),
        },
        "config": cfg,
        "side_bias": {
            "aff_win_rate": round(engine.aff_rate, 4),
            "elo_offset": round(engine.side_bias_elo, 1),
            "n": engine.side_n,
        },
        "metrics": metrics,
        "tournaments": [{
            "tourn_id": t["tourn_id"], "name": t["name"], "season": t.get("season"),
            "start": t.get("start"),
            "debates": sum(1 for d in debates if d["tourn_id"] == t["tourn_id"]),
        } for t in used_tourns],
        "defaults": {
            "rating": gl.DEFAULT_RATING, "rd": gl.DEFAULT_RD,
            "vol": gl.DEFAULT_VOL, "scale": gl.SCALE,
        },
    }
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)

    if not args.quiet:
        m = metrics["all"] or {}
        s = metrics["settled"] or {}
        print("built %d debaters (%d ranked) from %d debates across %d tournaments"
              % (meta["counts"]["debaters"], meta["counts"]["ranked"],
                 meta["counts"]["debates"], meta["counts"]["tournaments"]))
        print("seasons: %s" % ", ".join(seasons))
        print("aff win rate %.1f%% (%+.0f Elo)"
              % (engine.aff_rate * 100, engine.side_bias_elo))
        print("walk-forward accuracy  all: %.3f (n=%d)  settled: %.3f (n=%d)"
              % (m.get("accuracy", 0), m.get("n", 0),
                 s.get("accuracy", 0), s.get("n", 0)))
        print("brier %.4f   log loss %.4f" % (m.get("brier", 0), m.get("log_loss", 0)))
        print("wrote %s" % os.path.relpath(OUT, ROOT))


if __name__ == "__main__":
    main()
