"""Which student id owns which name.

Tabroom hands an entry's two students to us in two unrelated orders: the
team_results link is ascending by student id, the field list and entry-record
heading are in whatever order the tournament typed. Pairing the i-th name with
the i-th id therefore swapped partners, and because a person is named once at
first sighting, the swapped partner was registered under a name another student
already held -- one name on two people, and a real debater missing from the
board. Every fixture below is a pair actually observed in the 2025-26 data.

Run: python tests/test_entry_names.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from normalize import EntryNameResolver, PersonRegistry  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print("%-58s %s %s" % (name, "ok" if cond else "FAIL", detail))
    if not cond:
        FAILED.append(name)


def solve(*sightings):
    r = EntryNameResolver()
    for ids, names in sightings:
        r.observe(list(ids), list(names))
    return r.solve()


# North Texas: the same id pair listed both ways at different tournaments, plus
# one entry where Vargas debated with Ganesh instead.
got = solve(
    ((1702066, 1702187), ("Emanuel Vargas", "ronen bowman")),
    ((1702066, 1702187), ("ronen bowman", "Emanuel Vargas")),
    ((1555010, 1702066), ("Naren Ganesh", "Emanuel Vargas")),
)
check("flipped order does not decide identity",
      got.get(1702066) == "Emanuel Vargas", got.get(1702066, "-"))
check("the partner keeps their own name",
      got.get(1702187) == "Ronen Bowman", got.get(1702187, "-"))
check("neither student inherits the other's name",
      got.get(1702066) != got.get(1702187))
check("third student unaffected", got.get(1555010) == "Naren Ganesh")

# Kentucky: 1680822 is the one id common to both entries, so it is Rushing.
got = solve(
    ((1510475, 1680822), ("Wheezy Ervin", "Jonn Rushing")),
    ((1676854, 1680822), ("Jonn Rushing", "Haddie Schedler")),
)
check("shared id resolves to the shared name", got.get(1680822) == "Jonn Rushing")
check("partner is not relabelled Rushing", got.get(1676854) == "Haddie Schedler")

# Stanford siblings: same surname, so the entry alone cannot tell them apart --
# but Devin also debated with Yan, which settles both.
got = solve(
    ((1563202, 1563203), ("Kevin Lai", "Devin Lai")),
    ((1563203, 1765835), ("Devin Lai", "Frank Yan")),
)
check("siblings split by an outside entry", got.get(1563203) == "Devin Lai")
check("the other sibling gets the remaining name", got.get(1563202) == "Kevin Lai")

# Western Washington siblings who only ever debated together: order is genuinely
# unknowable, but they must stay two people rather than collapse into one.
got = solve(((1401132, 1736665), ("Abbigail Halpin", "William Halpin")))
check("inseparable pair still gets two distinct names",
      got.get(1401132) and got.get(1736665) and got[1401132] != got[1736665],
      "%s / %s" % (got.get(1401132, "-"), got.get(1736665, "-")))

# The old positional code, for contrast: it is what produced the duplicates.
def positional(ids, surnames, full):
    out = []
    for i, _sid in enumerate(ids):
        surname = surnames[i] if i < len(surnames) else ""
        name = ""
        if surname:
            for fn in full:
                if fn.split()[-1].lower() == surname.split()[-1].lower():
                    name = fn
                    break
        out.append(name or (full[i] if i < len(full) else surname))
    return out


check("old code did swap on a flipped field list",
      positional([1702066, 1702187], ["bowman", "Vargas"],
                 ["Emanuel Vargas", "ronen bowman"])
      == ["ronen bowman", "Emanuel Vargas"])
check("old code did collapse same-surname siblings",
      positional([1401132, 1736665], ["Halpin", "Halpin"],
                 ["Abbigail Halpin", "William Halpin"])
      == ["Abbigail Halpin", "Abbigail Halpin"])

# A settled name replaces the first-sighting guess; a rename override does not.
reg = PersonRegistry(overrides={"rename": {"t99": "Pinned Name"}})
reg.resolve("Wrong Guess", school="Emory", student_id=1)
check("set_name corrects a mislabelled student",
      reg.set_name("t1", "Right Name") and reg.people["t1"]["name"] == "Right Name")
reg.resolve("Whatever", school="Emory", student_id=99)
reg.set_name("t99", "Resolver Name")
check("a rename override still wins", reg.people["t99"]["name"] == "Pinned Name")

print()
if FAILED:
    print("FAILED: %s" % ", ".join(FAILED))
    sys.exit(1)
print("all entry-name checks passed")
