"""Scrape entries and round results for NDT/CEDA tournaments.

Requires a Tabroom session cookie -- see README, "Getting a cookie". Everything
is cached on disk by tabroom_client, so re-running is free and interrupting is
safe: a second run picks up exactly where the first stopped.

    python scripts/scrape.py --season 2025-26          # archive a whole season
    python scripts/scrape.py --tourn 36610             # one tournament
    python scripts/scrape.py --since 2026-09-01        # rolling: recent only
    python scripts/scrape.py --inspect 36610           # dump page structure

Tabroom's HTML differs between tournaments, tab-room software versions, and
event types, so the parsers here match on *header text* rather than on column
position, and fall back to scanning for recognisable shapes. Anything that
cannot be parsed confidently is skipped and reported rather than guessed at --
a silently mis-parsed round becomes a wrong rating that nobody can trace.

Writes data/processed/{people.json,debates.jsonl} and a scrape_report.json
naming every event and round that could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tabroom_client as tc  # noqa: E402
from normalize import (  # noqa: E402
    PersonRegistry, classify_division, is_elim, normalize_name,
)

ROOT = tc.ROOT
PROC = os.path.join(ROOT, "data", "processed")

TAG_RE = re.compile(r"<[^>]*>")
WS_RE = re.compile(r"\s+")
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)

ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&quot;": '"', "&#39;": "'", "&apos;": "'",
    "&ndash;": "-", "&mdash;": "-", "&lt;": "<", "&gt;": ">", "&rsquo;": "'",
    "&lsquo;": "'", "&ldquo;": '"', "&rdquo;": '"',
}


def text(fragment):
    out = TAG_RE.sub(" ", fragment or "")
    for k, v in ENTITIES.items():
        out = out.replace(k, v)
    out = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), out)
    return WS_RE.sub(" ", out).strip()


def cells(row_html):
    return [text(c) for c in CELL_RE.findall(row_html)]


def raw_cells(row_html):
    return CELL_RE.findall(row_html)


def tables(html):
    return TABLE_RE.findall(html)


def header_index(header_cells, *names):
    """Index of the first column whose header contains any of `names`."""
    low = [h.lower() for h in header_cells]
    for name in names:
        for i, h in enumerate(low):
            if name in h:
                return i
    return None


# --- events -------------------------------------------------------------------

EVENT_LINK_RE = re.compile(
    r'href="[^"]*?(?:event_id|event)=(\d+)[^"]*"[^>]*>\s*(.*?)\s*</a>', re.S | re.I)


def get_events(tourn_id):
    """All events at a tournament, with their division classification."""
    html = tc.fetch("/index/tourn/fields.mhtml?tourn_id=%d" % tourn_id)
    seen = {}
    for eid, label in EVENT_LINK_RE.findall(html):
        name = text(label)
        if not name or len(name) > 80:
            continue
        seen.setdefault(int(eid), name)
    return [{"event_id": eid, "name": name} for eid, name in seen.items()]


# --- entries ------------------------------------------------------------------

def get_entries(tourn_id, event_id):
    """Map entry code -> {school, debaters:[names]} for one event.

    Tabroom's field list is one row per entry, with the school, the entry code
    ("Michigan KM"), and the debaters' names in some order of columns that is
    not stable across tournaments -- hence the header matching.
    """
    html = tc.fetch("/index/tourn/fields.mhtml?tourn_id=%d&event_id=%d"
                    % (tourn_id, event_id))
    entries = {}
    for tbl in tables(html):
        rows = ROW_RE.findall(tbl)
        if len(rows) < 2:
            continue
        head = cells(rows[0])
        i_school = header_index(head, "school", "institution")
        i_code = header_index(head, "code", "entry", "team")
        i_names = header_index(head, "name", "debater", "student", "competitor")
        if i_code is None and i_names is None:
            continue
        for row in rows[1:]:
            cs = cells(row)
            if len(cs) < 2:
                continue
            code = cs[i_code] if i_code is not None and i_code < len(cs) else ""
            school = cs[i_school] if i_school is not None and i_school < len(cs) else ""
            names_blob = cs[i_names] if i_names is not None and i_names < len(cs) else ""
            names = split_names(names_blob)
            if not names and i_names is None:
                # Some field lists put both debaters in the code cell.
                names = split_names(code)
            if not code:
                code = "%s %s" % (school, "".join(n[:1] for n in names))
            if names:
                entries[code.strip()] = {"school": school.strip(), "debaters": names}
    return entries


NAME_SPLIT_RE = re.compile(r"\s*(?:&amp;|&|,\s*and\s+|\band\b|\+|/|;)\s*", re.I)


def split_names(blob):
    """Split a cell holding one or two debaters into individual names.

    Handles 'Smith & Jones', 'Smith, John and Jones, Mary', and newline-joined
    pairs. Returns [] when the cell clearly is not a name list.
    """
    if not blob:
        return []
    blob = blob.strip()
    if not blob or len(blob) > 120:
        return []
    parts = [p.strip() for p in NAME_SPLIT_RE.split(blob) if p.strip()]
    # "Smith, John" split on the comma above would give two fragments; rejoin
    # when a fragment is a bare surname followed by a bare given name.
    out = []
    for p in parts:
        if len(p) < 2 or not re.search(r"[A-Za-z]", p):
            continue
        out.append(p)
    return out[:2] if len(out) <= 2 else out[:2]


# --- rounds and results -------------------------------------------------------

ROUND_LINK_RE = re.compile(r'href="[^"]*?round_id=(\d+)[^"]*"[^>]*>\s*(.*?)\s*</a>', re.S | re.I)
WIN_RE = re.compile(r"\b(win|won|w)\b", re.I)
LOSS_RE = re.compile(r"\b(loss|lost|lose|l)\b", re.I)
BALLOT_RE = re.compile(r"\b([0-5])\s*[-–]\s*([0-5])\b")


def get_rounds(tourn_id, event_id):
    """Round ids and labels for one event, from the results index."""
    html = tc.fetch("/index/tourn/results/index.mhtml?tourn_id=%d&event_id=%d"
                    % (tourn_id, event_id))
    found = {}
    for rid, label in ROUND_LINK_RE.findall(html):
        name = text(label)
        if name:
            found.setdefault(int(rid), name)
    return [{"round_id": rid, "name": name} for rid, name in found.items()]


def get_round_pairings(round_id):
    """Parse one round's pairings into (aff_code, neg_code, winner) triples.

    Tabroom renders a completed round as a table with an aff column, a neg
    column, and some indication of who won -- sometimes a "Win"/"Loss" word,
    sometimes a ballot count like "3-0", sometimes bolding on the winner. We
    accept the first two and skip anything else rather than guess.
    """
    html = tc.fetch("/index/tourn/postings/round.mhtml?round_id=%d" % round_id)
    out = []
    for tbl in tables(html):
        rows = ROW_RE.findall(tbl)
        if len(rows) < 2:
            continue
        head = cells(rows[0])
        i_aff = header_index(head, "aff", "affirmative", "team 1")
        i_neg = header_index(head, "neg", "negative", "team 2")
        i_win = header_index(head, "win", "result", "decision", "vote", "ballot")
        if i_aff is None or i_neg is None:
            continue
        for row in rows[1:]:
            cs = cells(row)
            if max(i_aff, i_neg) >= len(cs):
                continue
            aff, neg = cs[i_aff].strip(), cs[i_neg].strip()
            if not aff or not neg or aff.lower() == "bye" or neg.lower() == "bye":
                continue
            winner = ballots = None
            if i_win is not None and i_win < len(cs):
                verdict = cs[i_win]
                m = BALLOT_RE.search(verdict)
                if m:
                    a, b = int(m.group(1)), int(m.group(2))
                    if a != b:
                        winner, ballots = ("aff" if a > b else "neg"), (a, b)
                elif re.search(r"\baff\b", verdict, re.I):
                    winner = "aff"
                elif re.search(r"\bneg\b", verdict, re.I):
                    winner = "neg"
            if winner is None:
                # Fall back to which side is marked as the winner in the markup.
                rc = raw_cells(row)
                if max(i_aff, i_neg) < len(rc):
                    aff_won = bool(re.search(r"<(b|strong)\b|winner", rc[i_aff], re.I))
                    neg_won = bool(re.search(r"<(b|strong)\b|winner", rc[i_neg], re.I))
                    if aff_won != neg_won:
                        winner = "aff" if aff_won else "neg"
            if winner:
                out.append({"aff": aff, "neg": neg, "winner": winner,
                            "ballots": ballots})
    return out


# --- orchestration ------------------------------------------------------------

def scrape_tournament(t, registry, debates, report, divisions):
    tid = t["tourn_id"]
    try:
        events = get_events(tid)
    except tc.AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad tournament must not stop the run
        report["failed_tournaments"].append({"tourn_id": tid, "name": t["name"],
                                             "error": str(exc)})
        return 0

    added = 0
    for ev in events:
        division = classify_division(ev["name"], t["name"])
        if division == "other":
            report["unclassified_events"].append(
                {"tourn_id": tid, "tournament": t["name"], "event": ev["name"]})
            continue
        if division not in divisions:
            continue

        try:
            entries = get_entries(tid, ev["event_id"])
            rounds = get_rounds(tid, ev["event_id"])
        except tc.AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            report["failed_events"].append({"tourn_id": tid, "event": ev["name"],
                                            "error": str(exc)})
            continue

        if not entries:
            report["empty_fields"].append({"tourn_id": tid, "event": ev["name"]})
            continue

        # code -> [person ids]
        roster = {}
        for code, info in entries.items():
            pids = [registry.resolve(n, school=info["school"], season=t.get("season"))
                    for n in info["debaters"]]
            pids = [p for p in pids if p]
            if len(pids) == 2:
                roster[normalize_code(code)] = pids

        for rd in rounds:
            try:
                pairings = get_round_pairings(rd["round_id"])
            except tc.AuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                report["failed_rounds"].append({"tourn_id": tid, "round": rd["name"],
                                                "error": str(exc)})
                continue
            for p in pairings:
                aff = roster.get(normalize_code(p["aff"]))
                neg = roster.get(normalize_code(p["neg"]))
                if not aff or not neg:
                    report["unmatched_entries"].append(
                        {"tourn_id": tid, "event": ev["name"], "round": rd["name"],
                         "aff": p["aff"], "neg": p["neg"]})
                    continue
                debates.append({
                    "tourn_id": tid, "season": t.get("season"), "date": t.get("start"),
                    "event": ev["name"], "division": division,
                    "round": rd["name"], "elim": is_elim(rd["name"]),
                    "aff": aff, "neg": neg, "winner": p["winner"],
                    "ballots_aff": p["ballots"][0] if p["ballots"] else None,
                    "ballots_neg": p["ballots"][1] if p["ballots"] else None,
                })
                added += 1
    return added


CODE_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def normalize_code(code):
    """Entry codes are typed by hand and drift between pages ('Michigan KM',
    'Michigan  K-M'), so compare them stripped of case and punctuation."""
    return CODE_CLEAN_RE.sub("", (code or "").lower())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", help="only this season, e.g. 2025-26")
    ap.add_argument("--tourn", type=int, action="append", help="specific tourn_id(s)")
    ap.add_argument("--since", help="only tournaments starting on/after this ISO date")
    ap.add_argument("--divisions", default="open,jv,novice",
                    help="which divisions to collect (default: all three)")
    ap.add_argument("--inspect", type=int, metavar="TOURN_ID",
                    help="print the structure of a tournament's pages and exit")
    ap.add_argument("--limit", type=int, help="stop after N tournaments")
    args = ap.parse_args()

    if args.inspect:
        inspect(args.inspect)
        return

    tourn_path = os.path.join(PROC, "tournaments.json")
    if not os.path.exists(tourn_path):
        raise SystemExit("run scripts/discover.py first")
    with open(tourn_path, "r", encoding="utf-8") as fh:
        tournaments = json.load(fh)

    if args.season:
        tournaments = [t for t in tournaments if t.get("season") == args.season]
    if args.tourn:
        tournaments = [t for t in tournaments if t["tourn_id"] in set(args.tourn)]
    if args.since:
        tournaments = [t for t in tournaments if (t.get("start") or "") >= args.since]
    tournaments.sort(key=lambda t: t.get("start") or "")
    if args.limit:
        tournaments = tournaments[:args.limit]

    if not tournaments:
        raise SystemExit("no tournaments matched those filters")

    divisions = {d.strip() for d in args.divisions.split(",") if d.strip()}
    registry = PersonRegistry()
    debates = []
    report = defaultdict(list)

    print("scraping %d tournaments (divisions: %s)"
          % (len(tournaments), ", ".join(sorted(divisions))))
    for i, t in enumerate(tournaments, 1):
        print("  [%2d/%d] %-52s " % (i, len(tournaments), t["name"][:52]), end="", flush=True)
        try:
            n = scrape_tournament(t, registry, debates, report, divisions)
        except tc.AuthError as exc:
            print("\n\nAUTH FAILED: %s" % exc)
            print("Nothing was lost -- everything fetched so far is cached. "
                  "Refresh the cookie and re-run the same command.")
            sys.exit(2)
        print("%5d rounds" % n)

    merge_and_write(debates, registry, report)


def merge_and_write(debates, registry, report):
    """Merge into any existing archive, keyed so re-scraping never duplicates."""
    os.makedirs(PROC, exist_ok=True)
    path = os.path.join(PROC, "debates.jsonl")

    existing = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    if not row.get("synthetic"):
                        existing.append(row)

    def key(d):
        return (d["tourn_id"], d.get("event"), d.get("round"),
                tuple(sorted(d["aff"])), tuple(sorted(d["neg"])))

    merged = {key(d): d for d in existing}
    for d in debates:
        merged[key(d)] = d
    rows = sorted(merged.values(), key=lambda d: (d.get("date") or "", d["tourn_id"]))

    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")

    people_path = os.path.join(PROC, "people.json")
    people = {}
    if os.path.exists(people_path):
        with open(people_path, "r", encoding="utf-8") as fh:
            people = json.load(fh)
    for pid, person in registry.people.items():
        if pid in people:
            for field in ("schools", "seasons"):
                merged_vals = people[pid].get(field, []) + [
                    v for v in person[field] if v not in people[pid].get(field, [])]
                people[pid][field] = merged_vals
        else:
            people[pid] = person
    with open(people_path, "w", encoding="utf-8") as fh:
        json.dump(people, fh, indent=1)

    report["collisions"] = registry.flag_collisions()
    with open(os.path.join(PROC, "scrape_report.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(report), fh, indent=1)

    print()
    print("%d debates on file (%d new this run), %d people"
          % (len(rows), len(debates), len(people)))
    for k in ("unclassified_events", "unmatched_entries", "empty_fields",
              "failed_rounds", "failed_events", "failed_tournaments"):
        if report.get(k):
            print("  %-22s %d  (see data/processed/scrape_report.json)"
                  % (k, len(report[k])))
    if report.get("collisions"):
        print("  %-22s %d  names carrying 3+ schools -- check for two people "
              "sharing a name" % ("possible collisions", len(report["collisions"])))
    print("\nnext: python scripts/build.py")


def inspect(tourn_id):
    """Print what the parsers can see. The fastest way to adapt to a page whose
    shape differs from what the parsers expect."""
    print("=== events at tournament %d ===" % tourn_id)
    events = get_events(tourn_id)
    for ev in events:
        print("  %-8s %-42s -> %s"
              % (ev["event_id"], ev["name"][:42], classify_division(ev["name"])))
    if not events:
        print("  (none found -- check the cookie, and dump the raw page below)")
        return

    ev = events[0]
    print("\n=== field list table headers, event %s (%s) ===" % (ev["event_id"], ev["name"]))
    html = tc.fetch("/index/tourn/fields.mhtml?tourn_id=%d&event_id=%d"
                    % (tourn_id, ev["event_id"]))
    for i, tbl in enumerate(tables(html)):
        rows = ROW_RE.findall(tbl)
        if rows:
            print("  table %d: %s" % (i, cells(rows[0])[:9]))
            for row in rows[1:3]:
                print("      %s" % (cells(row)[:9],))

    entries = get_entries(tourn_id, ev["event_id"])
    print("\n  parsed %d entries; first few:" % len(entries))
    for code, info in list(entries.items())[:5]:
        print("    %-22s %-22s %s" % (code, info["school"], info["debaters"]))

    rounds = get_rounds(tourn_id, ev["event_id"])
    print("\n=== rounds (%d) ===" % len(rounds))
    for rd in rounds[:12]:
        print("  %-9s %s" % (rd["round_id"], rd["name"]))
    if rounds:
        rid = rounds[0]["round_id"]
        print("\n=== round %s pairing tables ===" % rid)
        rhtml = tc.fetch("/index/tourn/postings/round.mhtml?round_id=%d" % rid)
        for i, tbl in enumerate(tables(rhtml)):
            rows = ROW_RE.findall(tbl)
            if rows:
                print("  table %d: %s" % (i, cells(rows[0])[:9]))
                for row in rows[1:3]:
                    print("      %s" % (cells(row)[:9],))
        pairings = get_round_pairings(rid)
        print("\n  parsed %d pairings; first few:" % len(pairings))
        for p in pairings[:5]:
            print("    %-24s vs %-24s -> %s %s"
                  % (p["aff"][:24], p["neg"][:24], p["winner"], p["ballots"] or ""))


if __name__ == "__main__":
    main()
