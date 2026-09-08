"""Name normalisation, person identity, and division classification.

Two messy problems live here.

**Identity.** The whole point of this project is rating individuals rather than
partnerships, so a debater has to keep one id across partner changes, school
transfers, and four years of Tabroom entries typed by different tab rooms. When
Tabroom exposes a numeric student id we use it and the problem disappears. When
it does not, we fall back to a normalised-name key, with a manual override file
for the two failure modes that fallback has: one person spelled two ways, and
two people sharing a name.

**Divisions.** Tabroom event names are free text entered by each tournament, so
the varsity division is variously "Open", "OPN", "CX-OP", "Open CX", "ocx",
"CardOP", "Varsity", and at round robins just the tournament's own name. The
classifier below is deliberately conservative: anything it cannot confidently
place lands in "other" and is excluded from the ranked pool rather than silently
polluting it.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERRIDES_DIR = os.path.join(ROOT, "data", "overrides")
ALIASES_FILE = os.path.join(OVERRIDES_DIR, "aliases.json")

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def normalize_name(raw):
    """Canonical 'firstname lastname' form for matching.

    Handles 'Last, First', accents, punctuation, generational suffixes, and the
    stray double spaces that tab rooms leave behind.
    """
    if not raw:
        return ""
    text = strip_accents(raw).strip()
    if "," in text:
        # "Smith, John" -> "John Smith". Only split on the first comma; a few
        # entries carry "Smith, John Jr." and the suffix strip below handles it.
        last, _, first = text.partition(",")
        text = "%s %s" % (first.strip(), last.strip())
    text = _PUNCT_RE.sub(" ", text.lower())
    parts = [p for p in _WS_RE.split(text) if p and p not in SUFFIXES]
    return " ".join(parts)


def name_key(raw):
    """The identity key we fall back to when there is no Tabroom student id."""
    return normalize_name(raw).replace(" ", "-")


def display_name(raw):
    """Human-facing form: 'John Smith', not 'Smith, John' or 'JOHN SMITH'."""
    if not raw:
        return ""
    text = _WS_RE.sub(" ", raw.strip())
    if "," in text:
        last, _, first = text.partition(",")
        last, first = last.strip(), first.strip()
        # "Smith, John Jr." -> the suffix belongs at the end, not in the middle.
        tokens = first.split()
        suffix = ""
        if tokens and tokens[-1].rstrip(".").lower() in SUFFIXES:
            suffix = " " + tokens[-1].rstrip(".")
            tokens = tokens[:-1]
        text = "%s %s%s" % (" ".join(tokens), last, suffix)
    text = _WS_RE.sub(" ", text).strip()
    # Only re-case strings that are entirely one case; leave "McGee" and
    # "van der Berg" alone when the source already got them right.
    if text.isupper() or text.islower():
        text = " ".join(w[:1].upper() + w[1:].lower() for w in text.split(" "))
    return text


# --- Division classification --------------------------------------------------

# Classification runs on the *full* event name from a tournament's own event
# list ("Open Policy", "Junior Varsity CX", "Novice Debate"), which is far more
# tractable than the abbreviations the circuit calendar shows. The abbreviation
# map below covers codes seen on this circuit whose full name is unavailable or
# still cryptic; exact matches here win over every pattern.
#
# Anything this file cannot place lands in "other" and is excluded from the
# ranked pool. scrape.py prints every unclassified event so the map can grow --
# silently guessing a division is far worse than declining to rate one.
EVENT_ABBREV = {
    # varsity / open policy
    "op": "open", "opn": "open", "open": "open", "o-pol": "open",
    "openpol": "open", "ocx": "open", "cx-o": "open", "cx-op": "open",
    "vcx": "open", "varsity": "open", "ceda": "open", "vceda": "open",
    "ndt": "open", "mac": "open", "qual": "open", "shir": "open",
    "d1": "open", "d2": "open", "d3": "open", "d4": "open",
    "d5": "open", "d6": "open", "d7": "open", "d8": "open",
    "1-open": "open",
    # junior varsity
    "jv": "jv", "cx-jv": "jv", "jvpol": "jv", "jceda": "jv", "j-pol": "jv",
    # novice
    "nov": "novice", "novi": "novice", "novic": "novice", "novice": "novice",
    "cx-n": "novice", "n-pol": "novice", "nceda": "novice", "3-nov": "novice",
    # explicitly not a rated policy division
    "x-obs": "other", "obsvr": "other", "obs": "other",
}

# Ordered: the first bucket whose pattern matches wins, so "JV" is tested before
# the looser varsity patterns and "Novice" before everything.
_DIVISION_RULES = [
    ("novice", re.compile(r"\b(nov(ice)?s?|rookie)\b|^nov", re.I)),
    ("jv", re.compile(r"\b(jv|junior\s*varsity)\b|^jv|^j\.v\.", re.I)),
    ("open", re.compile(
        r"\b(open|varsity|championship|champs|national\s*debate\s*tournament)\b"
        r"|^opn\b|^open", re.I)),
]

# Round robins and invitation-only varsity fields that name themselves.
_RR_RE = re.compile(r"\b(round\s*robin|rr|shirley|kickoff)\b", re.I)

# Formats that are not NDT/CEDA policy debate, or not a competitive division.
_EXCLUDE_RE = re.compile(
    r"\b(obs(e?rv(er|ation))?|x-?obs|scout|audience|exhibition"
    r"|ld|lincoln[- ]douglas|pf|public\s*forum|congress|student\s*congress"
    r"|parli|parliamentary|npda|npte|ipda|apda|nfa|british|worlds|bp"
    r"|speech|ie|individual\s*events|extemp|oratory|impromptu|interp"
    r"|duo|duet|poetry|prose|informative|persuasive|ads|di|hi|poi|poe"
    r"|middle\s*school|high\s*school|novice\s*ld)\b", re.I)


def classify_division(event_name, tournament_name=""):
    """Map a Tabroom event name to open / jv / novice / other.

    `tournament_name` is consulted only for round-robin style events whose own
    name is uninformative (an event literally called "RR").
    """
    name = (event_name or "").strip()
    if not name:
        return "other"

    exact = EVENT_ABBREV.get(_WS_RE.sub("", name).lower())
    if exact:
        return exact

    if _EXCLUDE_RE.search(name):
        return "other"
    for bucket, pattern in _DIVISION_RULES:
        if pattern.search(name):
            return bucket
    # A round robin on this circuit is an invite-only varsity field.
    if _RR_RE.search(name) or _RR_RE.search(tournament_name or ""):
        return "open"
    return "other"


def is_elim(round_name):
    """True for elimination rounds, which is where panels replace single judges."""
    if not round_name:
        return False
    name = round_name.lower()
    if re.search(r"\b(round|prelim|r)\s*\d+\b", name) or re.fullmatch(r"r?\d+", name.strip()):
        return False
    return bool(re.search(
        r"final|semi|quarter|octa|octo|double|triple|quad|elim|bracket"
        r"|\bdoubles\b|\btriples\b|\bpartial\b", name))


def round_sort_key(round_name):
    """Order rounds within a tournament: prelims by number, then elims by size."""
    name = (round_name or "").lower()
    prelim = re.search(r"(\d+)", name)
    if not is_elim(name):
        return (0, int(prelim.group(1)) if prelim else 0)
    order = ["triple", "double", "octa", "octo", "quarter", "semi", "final"]
    for i, token in enumerate(order):
        if token in name:
            # "finals" must not match before "quarterfinals"; the loop order
            # above already guarantees the coarser rounds are tested first.
            return (1, i)
    return (1, 99)


# --- Person registry ----------------------------------------------------------

class PersonRegistry:
    """Assigns and remembers stable person ids.

    Override file format (data/overrides/aliases.json):

        {
          "merge":  {"jon-smith": "john-smith"},
          "split":  {"john-smith": {"Wake Forest": "john-smith-wfu",
                                    "Michigan":    "john-smith-mich"}},
          "rename": {"john-smith": "John Smith"}
        }

    `merge` folds a stray spelling into a canonical id. `split` separates two
    people who share a name, keyed by the school on the entry. `rename` fixes a
    display name without touching identity.
    """

    def __init__(self, overrides=None):
        self.people = {}
        ov = overrides if overrides is not None else load_overrides()
        self.merge = ov.get("merge", {})
        self.split = ov.get("split", {})
        self.rename = ov.get("rename", {})

    def resolve(self, raw_name, school=None, student_id=None, season=None):
        """Return a stable person id, registering the person if new."""
        if student_id:
            pid = "t%s" % student_id
        else:
            pid = name_key(raw_name)
            if not pid:
                return None
            pid = self.merge.get(pid, pid)
            if pid in self.split and school:
                pid = self.split[pid].get(school, pid)

        person = self.people.get(pid)
        if person is None:
            person = {
                "id": pid,
                "name": self.rename.get(pid) or display_name(raw_name),
                "schools": [],
                "seasons": [],
                "tabroom_id": student_id,
            }
            self.people[pid] = person
        if school and school not in person["schools"]:
            person["schools"].append(school)
        if season and season not in person["seasons"]:
            person["seasons"].append(season)
        return pid

    def flag_collisions(self):
        """Name keys that carry several schools in the same season.

        Not proof of a collision -- transfers and hybrid teams do this -- but it
        is the right shortlist for a human to eyeball before trusting a ranking.
        """
        suspects = []
        for pid, person in self.people.items():
            if not person.get("tabroom_id") and len(person["schools"]) > 2:
                suspects.append({"id": pid, "name": person["name"],
                                 "schools": person["schools"]})
        return sorted(suspects, key=lambda s: -len(s["schools"]))


def load_overrides():
    if os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def ensure_overrides_file():
    os.makedirs(OVERRIDES_DIR, exist_ok=True)
    if not os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, "w", encoding="utf-8") as fh:
            json.dump({"merge": {}, "split": {}, "rename": {}}, fh, indent=2)
