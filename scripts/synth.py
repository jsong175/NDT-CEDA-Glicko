"""Generate a realistic synthetic season so the pipeline and dashboard can be
built and tested before (or without) live Tabroom data.

This is not decoration. It gives us a dataset where every debater's *true*
skill is known, which is the only way to check that the rating engine actually
recovers skill rather than merely producing plausible-looking numbers. See
tests/test_recovery.py.

The simulation mirrors the real circuit's shape: real tournament ids and dates
from the discovered calendar, power-matched prelims, elimination brackets whose
size scales with the field, partnerships that reshuffle between tournaments and
break up entirely between seasons, seniors graduating, and a small aff side
bias.

Anything written here is tagged synthetic:true so it can never be confused with
scraped data.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from normalize import PersonRegistry  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")

FIRST = """Alex Jordan Maya Chris Sam Priya Devon Noor Elena Marcus Kai Ravi Zoe
Tomas Aisha Liam Yuki Nadia Omar Sofia Ethan Leila Jonah Anika Diego Mei Caleb
Farah Isaac Rosa Malik Hana Theo Ines Nora Andre Vera Luis Talia Rowan Simone
Idris Petra Quinn Hugo Amara Felix Dara Otis Wren Cyrus Lena Miles Freya Amos
Gita Levi Suri Beau Nia Elias Tess Rafi Juno Silas Mira Dov Esme Kian Lyra""".split()

LAST = """Nguyen Patel Okafor Kim Rivera Cohen Alvarez Sharma Brooks Mensah Ito
Delgado Osei Larsen Haddad Whitfield Moreau Castillo Bhatt Fontaine Achebe Reyes
Sandoval Kowalski Abara Lindqvist Vasquez Underwood Rahman Petrov Barlow Ndiaye
Salazar Chowdhury Ferreira Grimaldi Halloran Ibrahim Jansen Kaur Lombardi
Maguire Novak Oyelaran Quintero Ramos Suzuki Tremblay Ustinov Villanueva Weber
Yamada Zeledon Ashford Bergstrom Calloway Duarte Eskildsen Fairbanks""".split()

SCHOOLS = ["Michigan", "Northwestern", "Harvard", "Emory", "Kentucky",
           "Georgetown", "Wake Forest", "Kansas", "Iowa", "Minnesota",
           "Michigan State", "Dartmouth", "California", "Texas", "Baylor",
           "Liberty", "Binghamton", "Rutgers", "Wayne State", "Oklahoma",
           "Houston", "Missouri State", "Gonzaga", "Vermont", "Trinity",
           "Samford", "Cornell", "Pittsburgh", "Indiana", "Wichita State",
           "James Madison", "Mary Washington", "West Virginia", "Nevada",
           "Arizona State", "Weber State", "New Mexico", "Navy", "Army",
           "Cal State Fullerton", "Long Beach", "UT San Antonio", "Wyoming",
           "Illinois State", "Kansas State", "Oregon", "Puget Sound"]

ELIM_NAMES = [(2, "Finals"), (4, "Semifinals"), (8, "Quarterfinals"),
              (16, "Octafinals"), (32, "Doubleoctafinals"), (64, "Triples")]


def logistic(x):
    return 1.0 / (1.0 + math.exp(-x))


class Debater:
    __slots__ = ("pid", "name", "school", "skill", "grad_season", "form")

    def __init__(self, pid, name, school, skill, grad_season):
        self.pid = pid
        self.name = name
        self.school = school
        self.skill = skill
        self.grad_season = grad_season
        self.form = 0.0


def make_field(rng, n, seasons):
    used = set()
    people = []
    for _ in range(n):
        for _attempt in range(50):
            name = "%s %s" % (rng.choice(FIRST), rng.choice(LAST))
            if name not in used:
                used.add(name)
                break
        school = rng.choice(SCHOOLS)
        # True skill: a long right tail, because the top of this circuit is
        # genuinely far above the middle of it.
        skill = rng.gauss(1500, 210) + max(0.0, rng.gauss(0, 130))
        grad = rng.choice(seasons + [None, None])
        people.append(Debater(None, name, school, skill, grad))
    return people


def pair_up(rng, roster):
    """Form two-person teams, preferring squadmates, as real squads do."""
    by_school = {}
    for d in roster:
        by_school.setdefault(d.school, []).append(d)
    teams, leftovers = [], []
    for _school, members in by_school.items():
        rng.shuffle(members)
        while len(members) >= 2:
            teams.append((members.pop(), members.pop()))
        leftovers.extend(members)
    rng.shuffle(leftovers)
    while len(leftovers) >= 2:  # hybrids
        teams.append((leftovers.pop(), leftovers.pop()))
    return teams


def team_strength(team):
    return sum(d.skill + d.form for d in team) / len(team)


def simulate_tournament(rng, teams, tourn, prelims, aff_bias, out, division):
    """Power-matched prelims followed by a bracket, emitting debate records."""
    records = {i: 0 for i in range(len(teams))}
    opponents = {i: set() for i in range(len(teams))}
    tid, when = tourn["tourn_id"], tourn.get("start")

    def play(i, j, round_name, elim, panel):
        a, b = teams[i], teams[j]
        # Coin-flip sides, as a real pairing does before elims decide by flip.
        if rng.random() < 0.5:
            i, j, a, b = j, i, b, a
        margin = (team_strength(a) + aff_bias - team_strength(b)) / 173.7178
        p_aff = logistic(margin)
        ballots_aff = sum(1 for _ in range(panel) if rng.random() < p_aff)
        aff_won = ballots_aff * 2 > panel
        out.append({
            "tourn_id": tid, "season": tourn.get("season"), "date": when,
            "event": division.title(), "division": division,
            "round": round_name, "elim": elim,
            "aff": [d.pid for d in a], "neg": [d.pid for d in b],
            "winner": "aff" if aff_won else "neg",
            "ballots_aff": ballots_aff, "ballots_neg": panel - ballots_aff,
            "panel": panel, "synthetic": True,
        })
        return i if aff_won else j

    for rd in range(1, prelims + 1):
        order = sorted(range(len(teams)), key=lambda i: (-records[i], rng.random()))
        unpaired = list(order)
        while len(unpaired) >= 2:
            i = unpaired.pop(0)
            partner_idx = 0
            # Prefer an opponent this team has not already hit.
            for k, cand in enumerate(unpaired):
                if cand not in opponents[i]:
                    partner_idx = k
                    break
            j = unpaired.pop(partner_idx)
            opponents[i].add(j)
            opponents[j].add(i)
            winner = play(i, j, "Round %d" % rd, False, 1)
            records[winner] += 1

    # Bracket: take the largest power of two that is at most a third of the field.
    seeds = sorted(range(len(teams)), key=lambda i: (-records[i], rng.random()))
    size = 1
    while size * 2 <= max(2, len(teams) // 3):
        size *= 2
    size = min(size, 64)
    if size < 2 or len(teams) < 8:
        return
    bracket = seeds[:size]
    for cut, label in sorted(ELIM_NAMES, key=lambda x: -x[0]):
        if cut > len(bracket):
            continue
        panel = 3 if cut > 4 else 5
        nxt = []
        for k in range(len(bracket) // 2):
            nxt.append(play(bracket[k], bracket[len(bracket) - 1 - k], label, True, panel))
        bracket = nxt
        if len(bracket) == 1:
            break


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20260908)
    ap.add_argument("--debaters", type=int, default=430)
    ap.add_argument("--out", default=PROC)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    tourn_path = os.path.join(PROC, "tournaments.json")
    if not os.path.exists(tourn_path):
        raise SystemExit("run scripts/discover.py first to get the real calendar")
    with open(tourn_path, "r", encoding="utf-8") as fh:
        tournaments = json.load(fh)

    seasons = sorted({t["season"] for t in tournaments})
    registry = PersonRegistry(overrides={})
    field = make_field(rng, args.debaters, seasons)
    for d in field:
        d.pid = registry.resolve(d.name, school=d.school)

    debates = []
    truth = {}
    for season in seasons:
        active = [d for d in field
                  if d.grad_season is None or d.grad_season >= season]
        season_tourns = [t for t in tournaments if t["season"] == season]
        season_tourns.sort(key=lambda t: t.get("start") or "")

        for t in season_tourns:
            # Field size: nationals and majors draw far more than a regional.
            big = any(k in t["name"].lower() for k in
                      ("national debate tournament", "ceda nationals", "shirley",
                       "wake", "kentucky", "northwestern", "georgetown"))
            small = "round robin" in t["name"].lower() or t.get(
                "divisions_listed", "").strip().upper() == "RR"
            frac = 0.10 if small else (0.42 if big else rng.uniform(0.14, 0.30))
            n_deb = max(16, int(len(active) * frac))
            attending = rng.sample(active, min(n_deb, len(active)))
            teams = pair_up(rng, attending)
            if len(teams) < 6:
                continue
            prelims = 8 if big else (5 if small else 6)
            for d in attending:
                # Form drifts slowly across a season; skill does not.
                d.form = max(-90.0, min(90.0, d.form + rng.gauss(0, 22)))
            simulate_tournament(rng, teams, t, prelims, aff_bias=18.0,
                                out=debates, division="open")
            for d in attending:
                registry.resolve(d.name, school=d.school, season=season)

        if season != seasons[-1]:
            # Summer between seasons: everyone improves a bit. This must not run
            # after the final season -- skill drift that happens after the last
            # debate is drift no rating could possibly have observed, and adding
            # it to `truth` would make the recovery test measure our clairvoyance
            # rather than our accuracy.
            for d in field:
                d.skill += rng.gauss(28, 34)
                d.form *= 0.3

    for d in field:
        truth[d.pid] = round(d.skill, 1)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "debates.jsonl"), "w", encoding="utf-8") as fh:
        for row in debates:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    with open(os.path.join(args.out, "people.json"), "w", encoding="utf-8") as fh:
        json.dump(registry.people, fh, indent=1)
    with open(os.path.join(args.out, "_synthetic_truth.json"), "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=1)

    print("synthetic dataset: %d debates, %d debaters, %d tournaments, seasons %s"
          % (len(debates), len(registry.people),
             len({d["tourn_id"] for d in debates}), ", ".join(seasons)))
    print("NOTE: every row is tagged synthetic:true -- replace with scrape.py output")


if __name__ == "__main__":
    main()
