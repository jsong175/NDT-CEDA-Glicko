"""Discover NDT/CEDA tournaments from the Tabroom circuit calendar.

The circuit calendar is one of the few Tabroom pages still readable without a
login, and it accepts a `year` parameter keyed to the fall of the season, so
`year=2025` returns the whole 2025-26 season including NDT and CEDA Nationals.

Writes data/processed/tournaments.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tabroom_client as tc  # noqa: E402

CIRCUIT_NDT_CEDA = 43
OUT = os.path.join(tc.ROOT, "data", "processed", "tournaments.json")

ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
LINK_RE = re.compile(r"tourn_id=(\d+)\"\s*>\s*(.*?)\s*</a>", re.S)
DATA_TEXT_RE = re.compile(r'data-text="([^"]*)"')
TAG_RE = re.compile(r"<[^>]*>")
WS_RE = re.compile(r"\s+")


def season_label(fall_year):
    return "%d-%02d" % (fall_year, (fall_year + 1) % 100)


def clean(fragment):
    text = TAG_RE.sub(" ", fragment)
    text = (text.replace("&ndash;", "-").replace("&amp;", "&")
                .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    return WS_RE.sub(" ", text).strip()


def parse_calendar(html, fall_year):
    """Pull (id, name, location, start date, divisions) out of the calendar table."""
    tournaments = []
    for row in ROW_RE.findall(html):
        link = LINK_RE.search(row)
        if not link:
            continue
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        stamps = DATA_TEXT_RE.findall(row)
        # The first data-text stamp on a row is the tournament start datetime.
        start = stamps[0][:10] if stamps else None
        tournaments.append({
            "tourn_id": int(link.group(1)),
            "name": clean(link.group(2)),
            "season": season_label(fall_year),
            "start": start,
            "location": cells[1] if len(cells) > 1 else "",
            "divisions_listed": cells[3] if len(cells) > 3 else "",
        })
    tournaments.sort(key=lambda t: (t["start"] or "", t["name"]))
    return tournaments


def discover(fall_years, circuit=CIRCUIT_NDT_CEDA):
    found = []
    for year in fall_years:
        path = "/index/circuit/calendar.mhtml?circuit_id=%d&year=%d" % (circuit, year)
        html = tc.fetch(path, allow_anonymous=True)
        rows = parse_calendar(html, year)
        print("  %s: %d tournaments" % (season_label(year), len(rows)))
        found.extend(rows)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", default="2025,2026",
                    help="comma-separated fall years, e.g. 2025,2026")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch the calendar even if cached (use for the live season)")
    args = ap.parse_args()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    if args.refresh:
        for year in years:
            url = "%s/index/circuit/calendar.mhtml?circuit_id=%d&year=%d" % (
                tc.BASE, CIRCUIT_NDT_CEDA, year)
            path = tc.cache_path(url)
            if os.path.exists(path):
                os.remove(path)

    print("Discovering NDT/CEDA tournaments...")
    tournaments = discover(years)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(tournaments, fh, indent=1)
    print("wrote %d tournaments -> %s" % (len(tournaments), os.path.relpath(OUT, tc.ROOT)))


if __name__ == "__main__":
    main()
