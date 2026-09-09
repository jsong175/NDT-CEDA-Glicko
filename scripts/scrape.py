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
    EntryNameResolver, PersonRegistry, classify_division, is_elim, normalize_name,
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

# Each entry row on the field list links to the pair's cumulative record at
# team_results.mhtml?id1=<student>&id2=<student>. Those ids are Tabroom's stable
# per-student identifiers -- the thing that lets us track a debater across
# partner changes -- so this link is the single most valuable field on the page.
TEAM_LINK_RE = re.compile(
    r"team_results\.mhtml\?id1=(\d+)(?:&(?:amp;)?id2=(\d+))?", re.I)


def get_entries(tourn_id, event_id):
    """Map entry code -> {school, ids:[student_id...], surnames:[...]} for an event.

    The NDT/CEDA field list is one row per entry, with columns that vary between
    tournaments, so columns are found by header text. The important columns are
    the entry Code ("Baylor BK"), used to join to the results pages, and the
    student ids in the row's team_results link, used for identity. The Entry
    column carries the two debaters' surnames, kept as a fallback for names.
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
        i_code = header_index(head, "code")
        i_entry = header_index(head, "entry", "name", "debater", "student", "team")
        if i_code is None:
            # No dedicated Code column: fall back to the Entry column as the key.
            i_code = i_entry
        if i_code is None:
            continue
        for row in rows[1:]:
            cs = cells(row)
            if len(cs) < 2 or i_code >= len(cs):
                continue
            code = cs[i_code].strip()
            if not code or code.lower() in ("code", "entry"):
                continue
            school = cs[i_school].strip() if i_school is not None and i_school < len(cs) else ""
            surnames = split_names(cs[i_entry]) if i_entry is not None and i_entry < len(cs) else []

            ids = []
            m = TEAM_LINK_RE.search(row)
            if m:
                ids = [int(g) for g in m.groups() if g]

            entries[code] = {"school": school, "ids": ids, "surnames": surnames}
    return entries


# The entry-record page carries a heading like "Jack Pacconi & Antonio Souchet".
# It's cached on disk, so resolving the same entry again -- across rounds,
# tournaments, or re-runs -- costs Tabroom nothing.
_TITLE_H_RE = re.compile(r"<h[1-5][^>]*>(.*?)</h[1-5]>", re.S | re.I)


def resolve_entry_names(tourn_id, entry_id, surnames=None):
    """Full debater names for one entry, aligned to `surnames` when given.

    Returns a list like ["Jack Pacconi", "Antonio Souchet"], or [] if the page
    can't be read (the caller then falls back to the field-list surnames).

    The record page has several headings -- the tournament name ("FR Shirley and
    ADA Fall Champions at WFU"), a location, section titles -- so we can't just
    grab the one containing "&". When we know the entry's surnames we accept only
    the heading whose parts end in exactly those surnames, which is unambiguous.
    """
    if not entry_id:
        return []
    try:
        html = tc.fetch("/index/tourn/postings/entry_record.mhtml?tourn_id=%d&entry_id=%d"
                        % (tourn_id, entry_id))
    except (tc.FetchError, tc.AuthError):
        return []

    want = sorted(s.split()[-1].lower() for s in surnames) if surnames else None
    for frag in _TITLE_H_RE.findall(html):
        heading = text(frag)
        if not ("&" in heading or " and " in heading.lower()) or len(heading) > 90:
            continue
        parts = [p.strip() for p in re.split(r"\s*&\s*|\s+and\s+", heading) if p.strip()]
        if want is not None:
            if sorted(p.split()[-1].lower() for p in parts) == want:
                return parts
        elif parts and all(" " in p and len(p.split()) <= 4 for p in parts) \
                and "schematic" not in heading.lower():
            # No surnames to check against: accept a plausibly name-shaped
            # heading, but only one made of "First Last" style parts.
            return parts
    return []


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

# Round results live at round_results.mhtml?tourn_id=..&round_id=.., linked from
# the event's results index. We take the round_id from those links specifically.
ROUND_LINK_RE = re.compile(
    r'round_results\.mhtml\?tourn_id=\d+&(?:amp;)?round_id=(\d+)"[^>]*>\s*(.*?)\s*</a>',
    re.S | re.I)
BALLOT_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
# Each Aff/Neg cell links to that entry's record page, which is the reliable
# source of both debaters' full names.
ENTRY_ID_RE = re.compile(r"entry_record\.mhtml\?tourn_id=\d+&(?:amp;)?entry_id=(\d+)", re.I)


def get_rounds(tourn_id, event_id):
    """Round ids and labels for one event, from the results index."""
    html = tc.fetch("/index/tourn/results/index.mhtml?tourn_id=%d&event_id=%d"
                    % (tourn_id, event_id))
    found = {}
    for rid, label in ROUND_LINK_RE.findall(html):
        name = text(label).replace(" Round results", "").strip()
        if name:
            found.setdefault(int(rid), name)
    return [{"round_id": rid, "name": name} for rid, name in found.items()]


def _clean_code(cell):
    """Strip the decorations Tabroom appends to a code in a results cell
    ('North Texas VB - ONLINE') so it joins to the field-list code."""
    cell = re.sub(r"\s*-\s*online\s*$", "", cell, flags=re.I)
    cell = re.sub(r"\s*\((?:online|hybrid|forfeit|fft?)\)\s*$", "", cell, flags=re.I)
    return cell.strip()


def _decide(verdict):
    """(winner, (aff_ballots, neg_ballots)) from a Win/Votes cell.

    Two shapes on this circuit:
      prelims: the cell is just "Aff" or "Neg"
      elims:   the cell is "5-0 AFF", "4-1 NEG", ...   (winner-loser ballots)
    """
    v = verdict.strip()
    low = v.lower()
    side = None
    if re.search(r"\baff\b", low):
        side = "aff"
    elif re.search(r"\bneg\b", low):
        side = "neg"

    m = BALLOT_RE.search(v)
    ballots = None
    if m:
        hi, lo = int(m.group(1)), int(m.group(2))
        if hi == lo:
            return None, None  # a tie/panel we can't read is not scoreable
        # The pair is winner-loser; if we couldn't read a side, infer nothing.
        if side == "aff":
            ballots = (max(hi, lo), min(hi, lo))
        elif side == "neg":
            ballots = (min(hi, lo), max(hi, lo))
    return side, ballots


def get_round_pairings(tourn_id, round_id):
    """Parse one round into {aff, neg, winner, ballots} rows, keyed by entry code.

    Anything whose winner cannot be read confidently is skipped rather than
    guessed at -- a mis-scored round is a wrong rating nobody can trace.
    """
    html = tc.fetch("/index/tourn/results/round_results.mhtml?tourn_id=%d&round_id=%d"
                    % (tourn_id, round_id))
    out = []
    for tbl in tables(html):
        rows = ROW_RE.findall(tbl)
        if len(rows) < 2:
            continue
        head = cells(rows[0])
        i_aff = header_index(head, "aff")
        i_neg = header_index(head, "neg")
        # "Win" is the decision on both page shapes; prefer it over "Votes".
        i_win = header_index(head, "win", "result", "decision")
        if i_win is None:
            i_win = header_index(head, "vote", "ballot")
        if i_aff is None or i_neg is None or i_win is None:
            continue
        for row in rows[1:]:
            cs = cells(row)
            rc = raw_cells(row)
            if max(i_aff, i_neg, i_win) >= len(cs):
                continue
            aff, neg = _clean_code(cs[i_aff]), _clean_code(cs[i_neg])
            if not aff or not neg or "bye" in (aff.lower(), neg.lower()):
                continue
            winner, ballots = _decide(cs[i_win])
            if not winner:
                continue

            def entry_id(idx):
                m = ENTRY_ID_RE.search(rc[idx]) if idx < len(rc) else None
                return int(m.group(1)) if m else None

            out.append({"aff": aff, "neg": neg, "winner": winner,
                        "ballots": ballots,
                        "aff_entry": entry_id(i_aff), "neg_entry": entry_id(i_neg)})
    return out


# --- orchestration ------------------------------------------------------------

def scrape_tournament(t, registry, debates, report, divisions, names):
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

        # normalized code -> entry info. Identity comes from the student ids in
        # the team_results link; names are resolved lazily below so we only fetch
        # a team_results page for entries that actually debated.
        by_code = {}
        for code, info in entries.items():
            if len(info.get("ids") or []) == 2:
                by_code[normalize_code(code)] = info

        resolved = {}  # normalized code -> [person id, person id], memoised

        def roster_lookup(code, entry_id):
            key = normalize_code(code)
            if key in resolved:
                return resolved[key]
            info = by_code.get(key)
            if not info:
                resolved[key] = None
                return None
            ids = info["ids"]
            surnames = info.get("surnames") or []
            full = resolve_entry_names(tid, entry_id, surnames)  # cached on disk

            # Identity is the student id, which is solid. The name is not: the
            # id1/id2 order and the heading order are independent, so we hand
            # both to the resolver and let it settle who is who once the whole
            # scrape is in (see EntryNameResolver). Until then a student is
            # provisionally labelled in page order, which is right about half
            # the time and always corrected below when it is not.
            names.observe(ids, full)

            pids = []
            for i, sid in enumerate(ids):
                name = full[i] if i < len(full) else ""
                if not name:
                    name = surnames[i] if i < len(surnames) else ""
                pids.append(registry.resolve(name or ("student %d" % sid),
                                             school=info["school"],
                                             student_id=sid, season=t.get("season")))
            pids = [p for p in pids if p]
            resolved[key] = pids if len(pids) == 2 else None
            return resolved[key]

        for rd in rounds:
            try:
                pairings = get_round_pairings(tid, rd["round_id"])
            except tc.AuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                report["failed_rounds"].append({"tourn_id": tid, "round": rd["name"],
                                                "error": str(exc)})
                continue
            for p in pairings:
                aff = roster_lookup(p["aff"], p.get("aff_entry"))
                neg = roster_lookup(p["neg"], p.get("neg_entry"))
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
    'Michigan  K-M'), and the field list tags online entries ('Houston MS -
    ONLINE') where the results pages do not, so compare them stripped of case,
    punctuation, and the online/hybrid marker."""
    code = _clean_code(code or "")
    code = CODE_CLEAN_RE.sub("", code.lower())
    return re.sub(r"(online|hybrid)$", "", code)


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
    names = EntryNameResolver()
    debates = []
    report = defaultdict(list)

    print("scraping %d tournaments (divisions: %s)"
          % (len(tournaments), ", ".join(sorted(divisions))))
    for i, t in enumerate(tournaments, 1):
        print("  [%2d/%d] %-52s " % (i, len(tournaments), t["name"][:52]), end="", flush=True)
        try:
            n = scrape_tournament(t, registry, debates, report, divisions, names)
        except tc.AuthError as exc:
            print("\n\nAUTH FAILED: %s" % exc)
            print("Nothing was lost -- everything fetched so far is cached. "
                  "Refresh the cookie and re-run the same command.")
            sys.exit(2)
        print("%5d rounds" % n)

    fixed = apply_resolved_names(registry, names, report)
    if fixed:
        print("resolved %d debater name(s) from cross-entry evidence" % fixed)

    merge_and_write(debates, registry, report)


def apply_resolved_names(registry, names, report):
    """Relabel every student the resolver could pin down.

    Left uncorrected, a swapped name does not just mislabel one person: the
    partner is registered under a name someone else already holds, so the board
    shows two people with one name and the real debater never appears.
    """
    fixed = 0
    for sid, name in names.solve().items():
        pid = "t%s" % sid
        if registry.set_name(pid, name):
            fixed += 1
        # Mark it settled either way, so merge_and_write knows this name is
        # backed by cross-entry evidence and may overwrite what is on file.
        if pid in registry.people:
            registry.people[pid]["name_resolved"] = True

    # Two ids sharing a name after this means nobody debated apart from their
    # partner all season -- worth a human look, not an error.
    seen = defaultdict(list)
    for pid, person in registry.people.items():
        seen[normalize_name(person["name"])].append(pid)
    report["shared_names"] = [
        {"name": registry.people[pids[0]]["name"], "ids": pids}
        for pids in seen.values() if len(pids) > 1]
    return fixed


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
            # A name the resolver settled this run beats whatever an earlier,
            # thinner run wrote; an unresolved guess never overwrites a
            # settled name.
            if person.get("name_resolved"):
                people[pid]["name"] = person["name"]
                people[pid]["name_resolved"] = True
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
              "failed_rounds", "failed_events", "failed_tournaments",
              "shared_names"):
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
    print("\n  parsed %d entries; first few (ids + field-list surnames):" % len(entries))
    for code, info in list(entries.items())[:5]:
        print("    %-20s ids=%-18s %s"
              % (code, info.get("ids"), " & ".join(info.get("surnames") or [])))

    rounds = get_rounds(tourn_id, ev["event_id"])
    print("\n=== rounds (%d) ===" % len(rounds))
    for rd in rounds[:14]:
        print("  %-9s %s" % (rd["round_id"], rd["name"]))
    if rounds:
        rid = rounds[-1]["round_id"]  # a prelim, the common case
        print("\n=== '%s' (round %s) results table ===" % (rounds[-1]["name"], rid))
        rhtml = tc.fetch(
            "/index/tourn/results/round_results.mhtml?tourn_id=%d&round_id=%d"
            % (tourn_id, rid))
        for i, tbl in enumerate(tables(rhtml)):
            rows = ROW_RE.findall(tbl)
            if rows:
                print("  table %d header: %s" % (i, cells(rows[0])[:8]))
        codes = {normalize_code(c) for c in entries}
        for label, rd in ((rounds[-1]["name"], rounds[-1]),
                          (rounds[0]["name"], rounds[0])):
            pairings = get_round_pairings(tourn_id, rd["round_id"])
            hits = sum(1 for p in pairings
                       if normalize_code(p["aff"]) in codes
                       and normalize_code(p["neg"]) in codes)
            print("\n  %-8s parsed %d pairings, %d matching the field list; first few:"
                  % (label, len(pairings), hits))
            for p in pairings[:4]:
                print("    %-22s vs %-22s -> %s %s"
                      % (p["aff"][:22], p["neg"][:22], p["winner"], p["ballots"] or ""))


if __name__ == "__main__":
    main()
