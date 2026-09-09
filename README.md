# NDT/CEDA Glicko

Individual Glicko-2 ratings for college policy debaters on the NDT/CEDA circuit,
plus a head-to-head win-probability calculator, published as a static dashboard
on GitHub Pages.

Ratings are **per person, not per partnership**. Partnerships churn constantly —
people switch partners mid-season, graduate, transfer, or debate with three
different people in a year — so every round is credited to the four individuals
in it, and a "team" rating is assembled on demand from whichever two people you
name. That is what makes it possible to ask *"how would this pairing do against
that one"* for two people who have never actually debated together.

---

## Quick start

No dependencies beyond Python 3.9+. Nothing to install.

```bash
python scripts/discover.py --years 2025,2026   # find tournaments (no login needed)
python scripts/synth.py                        # sample data, so you can see it work
python scripts/build.py                        # compute ratings -> docs/data/
python -m http.server 8000 --directory docs    # open http://localhost:8000
```

That gets you a fully working dashboard on simulated data, with a loud banner
saying so. To swap in real results, add a Tabroom cookie and run `scrape.py` —
see below.

---

## Getting a Tabroom cookie

Tabroom put results and pairings behind a login in 2026. You need a session
cookie from your own account:

1. Log in at [tabroom.com](https://www.tabroom.com).
2. Open DevTools → **Application** (Chrome/Edge) or **Storage** (Firefox) →
   **Cookies** → `https://www.tabroom.com`.
3. Copy the value of **`TabroomToken`**.
4. Save it:

```bash
echo "PASTE_THE_TOKEN_HERE" > data/.tabroom_cookie
python scripts/tabroom_client.py check     # confirms it opens a gated page
```

`data/.tabroom_cookie` is gitignored. You can also set `TABROOM_COOKIE` in the
environment instead. Cookies expire every few weeks; `check` tells you when.

### Please read this before scraping

`robots.txt` disallows `/index/tourn/results` and `/index/tourn/postings`, and
Tabroom added the login wall specifically because of load from AI crawlers. This
tool is written to be as close to harmless as a scraper can be — one request at a
time, a minimum 1.25s gap, every page cached on disk and **never re-fetched**, so
a full season costs Tabroom about what one person browsing for an afternoon costs
it, and re-running the pipeline costs nothing. Completed tournaments are
immutable, so the rolling update only ever touches the current season.

That is a mitigation, not a permission. If you plan to run this continuously or
publish it widely, the right move is to ask Tabroom for access
(help@tabroom.com) and switch the client over to whatever they give you.

---

## Going from sample data to real ratings

Once the cookie is in place, one command does everything — verifies auth, checks
the parsers against a real tournament, scrapes, rates, tests, and publishes:

```bash
python scripts/run_all.py --season 2025-26 --push
```

It stops at the first thing that looks wrong rather than pressing on. The parser
check is the step that matters: the scrapers were written without ever seeing a
logged-in page, so the first real run is where they get verified. If it can't
read entries, rounds, or pairings — or if entry codes don't match between the
field list and the pairings — it prints what it *did* see and exits before
touching the other forty tournaments.

---

## Scraping and building

```bash
# One tournament, to check the parsers against a real page
python scripts/scrape.py --inspect 36610
python scripts/scrape.py --tourn 36610

# A whole season (safe to interrupt and re-run — it resumes from cache)
python scripts/scrape.py --season 2025-26 --divisions open

# Rolling: only tournaments since a date
python scripts/scrape.py --since 2026-08-01 --divisions open

python scripts/build.py
```

`scrape.py` writes `data/processed/scrape_report.json` listing every event and
round it could **not** read confidently — unclassified divisions, entries whose
codes didn't match between the field list and the pairings, and names that look
like two different people sharing a spelling. Nothing is ever guessed at; a
silently mis-parsed round becomes a wrong rating nobody can trace. Skim that file
before trusting a fresh ranking.

Fix identity problems in `data/overrides/aliases.json`:

```json
{
  "merge":  {"jon-smith": "john-smith"},
  "split":  {"john-smith": {"Wake Forest": "john-smith-wfu", "Michigan": "john-smith-mich"}},
  "rename": {"john-smith": "John Smith"}
}
```

---

## How the rating works

**Glicko-2**, not plain Elo. Each debater carries three numbers instead of one:

| | |
|---|---|
| **Rating** | the skill estimate, starting at 1500 |
| **RD** | how unsure we are — true skill is within `rating ± 2 RD` about 95% of the time. Shrinks as you debate, widens while you don't |
| **Volatility** | how erratic results have been, which controls how violently the rating may swing |

That matters for exactly the cases this circuit is full of: a first-year with
four rounds on record, and a senior returning in February after a fall away.

**Getting individuals out of 2-on-2 rounds.** A team's strength is the average of
its two debaters. The wrinkle is that you are only *half* of your team, so your
rating moves the outcome only half as much as it would one-on-one. The update
handles this explicitly: expectations are formed at team level, and each partner
absorbs the surprise at slope `1/2` (`glicko2.build_team_games`).

This is not a detail. Skipping it — judging each debater individually against the
opposing *team* — punishes strong debaters for carrying weaker partners, rewards
the partner, and in testing against known simulated skill it compressed the whole
rating scale to less than half its true spread (rank correlation 0.77 → 0.88).

**Rating periods.** One tournament is one period. Every round you take there is
scored against the ratings as they stood *before* the tournament, so a
tournament's own results never contaminate each other, and the order rounds are
processed in doesn't matter.

**Layoffs and seasons.** RD widens by the *month* of inactivity, not by the
tournament — there are ~40 tournaments a year and nobody attends more than a
third of them, so per-tournament decay would pin everyone at the RD ceiling by
November. Between seasons every rating is pulled 25% back toward 1500 and RD is
reopened to at least 150.

**Side bias** is measured from the data rather than assumed, and folded into both
predictions and updates.

**Who gets ranked.** A debater is provisional — kept off the default board —
under `min_rounds_ranked` (12) rounds, or with an RD still wider than
`provisional_rd` (200). Those two are meant to say the same thing, so the RD bar
is set near the uncertainty a debater actually carries at the round minimum
(~208 in the 2025-26 pool). It must also stay above `season_rd_floor`: every
rating is reopened to at least that floor when a season turns over, so a cutoff
underneath it marks the entire pool provisional every September and empties the
board until midseason. `load_config()` refuses that combination outright.

Everything above is configurable in `config.json`.

---

## Does it work?

`tests/` is the answer, and it is worth running:

```bash
python tests/test_glicko2.py     # engine, incl. Glickman's own worked example
python tests/test_pipeline.py    # output integrity (runs against real data too)
python tests/test_js_parity.py   # the dashboard's JS maths == the Python engine
python tests/test_entry_names.py # which student id owns which name
python tests/test_recovery.py    # does it recover known latent skill?
```

`test_recovery.py` is the important one. The synthetic dataset knows every
debater's true skill, so it can check the thing that actually matters — whether
the ratings measure skill or merely look plausible. On the sample data:

| | |
|---|---|
| Rank correlation with true skill | **0.88** |
| Top-25 rated who are truly top-50 | **92%** |
| Walk-forward accuracy | **63.8%** (67.3% when both teams are settled) |
| Brier score | **0.223** (0.25 = coin flip) |

Those predictions are all made *before* the tournament they belong to is rated —
walk-forward, not in-sample fit. Real-data numbers will differ and the dashboard
recomputes them on every build; the Method tab always shows the current figures
with a calibration table.

---

## Publishing to GitHub Pages

The dashboard is three static files plus JSON, served straight out of `docs/`.

```bash
git remote add origin https://github.com/jsong175/NDT-CEDA-Glicko.git
git push -u origin main
```

Then **Settings → Pages → Source: Deploy from a branch → `main` / `docs`**. The
site lands at `https://jsong175.github.io/NDT-CEDA-Glicko/`.

### Rolling updates

`.github/workflows/update.yml` runs every Monday at 11:00 UTC: refreshes the
calendar, scrapes anything new, rebuilds, runs the tests, and commits `docs/data`
if the numbers changed. Add your cookie as repository secret **`TABROOM_COOKIE`**
(Settings → Secrets and variables → Actions). Without it the workflow still
rebuilds from committed data and logs a warning instead of failing. It also
caches Tabroom pages between runs, which is what keeps the load near zero.

Trigger it by hand from the Actions tab any time — there's a `rebuild_only`
option for re-running the maths after a config change.

---

## Layout

```
scripts/
  glicko2.py        rating engine: Glicko-2 + the team -> individual extension
  ratings.py        pipeline: debates -> ratings, with walk-forward scoring
  build.py          writes docs/data/ for the dashboard
  discover.py       circuit calendar -> tournaments.json   (no login needed)
  scrape.py         entries + round results -> debates.jsonl
  tabroom_client.py cached, rate-limited, cookie-authenticated HTTP
  normalize.py      name/identity resolution, division classification
  synth.py          simulated circuit for testing without live data
data/
  processed/        tournaments.json, people.json, debates.jsonl
  overrides/        manual identity fixes
  raw/cache/        page cache (gitignored)
docs/               the dashboard — GitHub Pages serves this directory
```

### Debate record schema

`data/processed/debates.jsonl`, one JSON object per line:

```json
{"tourn_id": 36610, "season": "2025-26", "date": "2025-11-15",
 "event": "Open", "division": "open", "round": "Round 3", "elim": false,
 "aff": ["jane-doe", "sam-lee"], "neg": ["alex-ruiz", "mia-chen"],
 "winner": "aff", "ballots_aff": 1, "ballots_neg": 0}
```

Any source that can produce that shape works — the rating engine never touches
Tabroom.

---

## Known limits

- **Judges are not modelled.** A rating blends debating skill with how well
  someone's style matched the judges they happened to draw.
- **Open/Varsity only** by default. JV and novice rounds are collected but not
  ranked: those pools barely cross over with Open, so their ratings would drift
  on an unanchored scale. Change `divisions` in `config.json` if you want them.
- **Ratings are relative.** 1500 is the pool average, not an absolute standard,
  and it is not comparable across circuits or eras.
- **Parsers need verification.** Tabroom's HTML varies between tournaments and
  tab-software versions. The parsers match on header text rather than column
  position and skip anything ambiguous, but run `--inspect` on a new tournament
  before trusting a bulk scrape.
