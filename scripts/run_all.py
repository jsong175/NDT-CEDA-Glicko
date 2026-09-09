"""One command to take the site from sample data to real ratings.

    python scripts/run_all.py

Runs the whole chain: verify the cookie, sanity-check the parsers against one
real tournament, scrape, rate, test, and optionally commit and push. It stops at
the first thing that looks wrong rather than pressing on, because the expensive
failure here is not a crash -- it is scraping forty tournaments into a plausible
looking but silently wrong ranking.

The parser check is the important step. The scrapers were written without ever
seeing a logged-in page, so the first real run is where they get verified. If
the check cannot read entries or pairings, this script prints what it *did* see
and exits, so the parsers can be fixed against real HTML before anything else
runs.

Options:
    --season 2025-26      which season to archive (default: both)
    --sample 36610        tournament used for the parser check
    --skip-check          go straight to scraping (only after a clean check)
    --push                commit and push the result when everything passes
    --yes                 don't ask for confirmation before the full scrape
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scrape  # noqa: E402
import tabroom_client as tc  # noqa: E402
from normalize import classify_division  # noqa: E402

ROOT = tc.ROOT
PROC = os.path.join(ROOT, "data", "processed")


def rule(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def run(cmd, **kw):
    print("$ " + " ".join(cmd))
    return subprocess.run([sys.executable] + cmd, cwd=ROOT, **kw)


def step_auth():
    rule("1/6  Checking the Tabroom cookie")
    cookie = tc.load_cookie()
    if not cookie:
        print("No cookie found.\n")
        print("  Log in at tabroom.com, then DevTools -> Application -> Cookies")
        print("  -> https://www.tabroom.com -> copy the TabroomToken value, then:\n")
        print('      echo "PASTE_TOKEN_HERE" > data/.tabroom_cookie\n')
        print("  (or set the TABROOM_COOKIE environment variable)")
        return False
    ok, detail = tc.check_auth()
    print(("  OK: " if ok else "  FAILED: ") + detail)
    if not ok:
        print("\n  The cookie is present but did not open a gated page. It has")
        print("  most likely expired -- log in again and copy a fresh one.")
    return ok


def step_parser_check(sample_id):
    """Prove the parsers work on real HTML before scraping anything in bulk."""
    rule("2/6  Verifying the parsers against tournament %d" % sample_id)
    try:
        events = scrape.get_events(sample_id)
    except Exception as exc:  # noqa: BLE001
        print("  Could not read the event list: %s" % exc)
        return False

    print("  events found: %d" % len(events))
    for ev in events[:12]:
        print("    %-8s %-44s -> %s"
              % (ev["event_id"], ev["name"][:44], classify_division(ev["name"])))
    if not events:
        print("\n  No events parsed. Run:  python scripts/scrape.py --inspect %d"
              % sample_id)
        return False

    rated = [e for e in events if classify_division(e["name"]) != "other"]
    if not rated:
        print("\n  No event classified into a rated division. Either this")
        print("  tournament has none, or normalize.EVENT_ABBREV needs the codes")
        print("  listed above adding to it.")
        return False

    ev = rated[0]
    try:
        entries = scrape.get_entries(sample_id, ev["event_id"])
        rounds = scrape.get_rounds(sample_id, ev["event_id"])
    except Exception as exc:  # noqa: BLE001
        print("  Could not read entries/rounds: %s" % exc)
        return False

    print("\n  entries parsed: %d" % len(entries))
    for code, info in list(entries.items())[:5]:
        print("    %-22s %-20s %s" % (code[:22], info["school"][:20], info["debaters"]))
    print("  rounds parsed:  %d" % len(rounds))
    for rd in rounds[:6]:
        print("    %-9s %s" % (rd["round_id"], rd["name"]))

    if not entries:
        print("\n  Entries came back empty -- the field-list parser needs fixing.")
        print("  Run:  python scripts/scrape.py --inspect %d" % sample_id)
        return False
    if not rounds:
        print("\n  No rounds found -- the results-index parser needs fixing.")
        print("  Run:  python scripts/scrape.py --inspect %d" % sample_id)
        return False

    pairings = scrape.get_round_pairings(rounds[0]["round_id"])
    print("\n  pairings parsed from '%s': %d" % (rounds[0]["name"], len(pairings)))
    for p in pairings[:5]:
        print("    %-24s vs %-24s -> %s %s"
              % (p["aff"][:24], p["neg"][:24], p["winner"], p["ballots"] or ""))
    if not pairings:
        print("\n  No pairings parsed -- the round parser needs fixing.")
        print("  Run:  python scripts/scrape.py --inspect %d" % rounds[0]["round_id"])
        return False

    # Do the entry codes in the pairings actually match the field list? If not,
    # every round would be dropped as unmatched and the ranking would be empty.
    codes = {scrape.normalize_code(c) for c in entries}
    hits = sum(1 for p in pairings
               if scrape.normalize_code(p["aff"]) in codes
               and scrape.normalize_code(p["neg"]) in codes)
    print("\n  pairings whose entry codes match the field list: %d/%d"
          % (hits, len(pairings)))
    if hits < len(pairings) * 0.5:
        print("\n  Most pairings do not match the field list, so most rounds")
        print("  would be silently dropped. The two pages are probably using")
        print("  different code formats -- fix normalize_code() before scraping.")
        return False

    print("\n  Parsers look healthy.")
    return True


def step_scrape(season, assume_yes):
    rule("3/6  Scraping results")
    with open(os.path.join(PROC, "tournaments.json"), encoding="utf-8") as fh:
        tournaments = json.load(fh)
    if season:
        tournaments = [t for t in tournaments if t.get("season") == season]
    n = len(tournaments)
    print("  %d tournaments to archive." % n)
    print("  At ~1.3s per request this is a slow, deliberate crawl; it is")
    print("  resumable and every page is cached, so interrupting is safe.")
    if not assume_yes:
        try:
            reply = input("\n  Proceed? [y/N] ").strip().lower()
        except EOFError:
            reply = "y"
        if reply not in ("y", "yes"):
            print("  Stopped.")
            return False
    cmd = ["scripts/scrape.py", "--divisions", "open"]
    if season:
        cmd += ["--season", season]
    return run(cmd).returncode == 0


def step_build():
    rule("4/6  Computing ratings")
    return run(["scripts/build.py"]).returncode == 0


def step_tests():
    rule("5/6  Running tests")
    ok = True
    for t in ("test_glicko2", "test_pipeline", "test_js_parity"):
        r = run(["tests/%s.py" % t], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        print("  %-18s %s" % (t, "PASS" if r.returncode == 0 else "FAIL"))
        ok = ok and r.returncode == 0
    return ok


def step_report():
    rule("6/6  Result")
    with open(os.path.join(ROOT, "docs", "data", "meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    c, m = meta["counts"], meta["metrics"]["all"]
    print("  synthetic data present : %s" % meta["synthetic"])
    print("  seasons                : %s" % ", ".join(meta["seasons"]))
    print("  debaters rated / ranked: %d / %d" % (c["debaters"], c["ranked"]))
    print("  rounds                 : %d" % c["debates"])
    print("  tournaments            : %d" % c["tournaments"])
    print("  aff win rate           : %.1f%%" % (meta["side_bias"]["aff_win_rate"] * 100))
    print("  walk-forward accuracy  : %.1f%%" % (m["accuracy"] * 100))
    print("  brier / log loss       : %.4f / %.4f" % (m["brier"], m["log_loss"]))

    report_path = os.path.join(PROC, "scrape_report.json")
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as fh:
            rep = json.load(fh)
        flagged = {k: len(v) for k, v in rep.items() if v}
        if flagged:
            print("\n  Things the scraper could not read confidently:")
            for k, v in sorted(flagged.items()):
                print("    %-22s %d" % (k, v))
            print("  Details: data/processed/scrape_report.json")
    if meta["synthetic"]:
        print("\n  NOTE: simulated rounds are still in the dataset. Delete")
        print("  data/processed/debates.jsonl and re-scrape to clear them.")
    return meta


def step_push(meta):
    rule("Publishing")
    subprocess.run(["git", "add", "docs/data", "data/processed"], cwd=ROOT)
    msg = ("Real results: %d debaters, %d rounds, %d tournaments"
           % (meta["counts"]["debaters"], meta["counts"]["debates"],
              meta["counts"]["tournaments"]))
    r = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT)
    if r.returncode != 0:
        print("  Nothing to commit.")
        return
    subprocess.run(["git", "push"], cwd=ROOT)
    print("\n  https://jsong175.github.io/NDT-CEDA-Glicko/")
    print("  Pages usually rebuilds within a minute.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", default=None)
    ap.add_argument("--sample", type=int, default=36610,
                    help="tournament used for the parser check "
                         "(default: Wake 2025, a large well-formed field)")
    ap.add_argument("--skip-check", action="store_true")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args()

    started = time.time()
    if not step_auth():
        sys.exit(1)
    if not args.skip_check and not step_parser_check(args.sample):
        print("\nStopping before the bulk scrape. Nothing was wasted -- the pages")
        print("fetched so far are cached, so fixing the parsers and re-running")
        print("costs Tabroom nothing.")
        sys.exit(1)
    if not step_scrape(args.season, args.yes):
        sys.exit(1)
    if not step_build():
        sys.exit(1)
    tests_ok = step_tests()
    meta = step_report()
    if not tests_ok:
        print("\nTests failed -- not publishing. Investigate before pushing.")
        sys.exit(1)
    if args.push:
        step_push(meta)
    print("\ndone in %.0fs" % (time.time() - started))


if __name__ == "__main__":
    main()
