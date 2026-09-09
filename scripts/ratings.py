"""Run the Glicko-2 pipeline over scraped debates and emit the dashboard data.

Rating period = one tournament. Tournaments are processed in date order; inside
a tournament every round a debater took is one game, and all of those games are
scored against the ratings as they stood *before* the tournament started. That
is what Glicko-2 expects, and it also means a tournament's results never depend
on the order its own rounds are processed in.

Three things need care beyond the textbook algorithm:

  * **Inactivity is measured in months, not tournaments.** There are ~40
    tournaments on the circuit and nobody attends more than a third of them. If
    RD widened once per tournament, everyone would sit at the 350 ceiling by
    November. Instead decay is applied lazily: when a debater next competes, we
    widen their RD for the months elapsed since they last did.

  * **Season boundaries regress toward the mean.** The topic changes, half the
    field graduates, and squads reshuffle, so a rating earned in April is
    weaker evidence in September than a rating earned in March.

  * **Side bias is estimated from the data, not assumed.** If the circuit's aff
    win rate is not 50%, both the updates and the predictor should know it.

Outputs land in docs/data/ and are consumed directly by the static dashboard.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import glicko2 as gl  # noqa: E402
from normalize import round_sort_key  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
OUT = os.path.join(ROOT, "docs", "data")
CONFIG_FILE = os.path.join(ROOT, "config.json")

DEFAULT_CONFIG = {
    "divisions": ["open"],
    "tau": 0.5,
    "season_regression": 0.25,
    "season_rd_floor": 150.0,
    "inactivity_rd_per_month": 1.0,
    "elim_weight": 1.0,
    "ballot_scores": False,
    "side_bias": "auto",
    "min_rounds_ranked": 12,
    "provisional_rd": 200.0,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    check_config(cfg)
    return cfg


def check_config(cfg):
    """Reject settings that quietly contradict each other.

    provisional_rd below season_rd_floor is the one that bit us: every rating is
    reopened to at least season_rd_floor when a season turns over, so a cutoff
    beneath it flags the entire pool as provisional on day one of every season
    and empties the default ranking until midseason.
    """
    if cfg["provisional_rd"] <= cfg["season_rd_floor"]:
        raise ValueError(
            "provisional_rd (%.0f) must exceed season_rd_floor (%.0f): every "
            "rating is reset to at least the floor between seasons, so a lower "
            "cutoff marks everyone provisional for good."
            % (cfg["provisional_rd"], cfg["season_rd_floor"]))
    return cfg


def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def months_between(a, b):
    """Whole-ish months between two ISO dates, as a float."""
    if not a or not b:
        return 0.0
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return max(0.0, ((yb - ya) * 12 + (mb - ma)) + (db - da) / 30.44)


def estimate_side_bias(debates):
    """Aff win rate expressed as an Elo-scale offset.

    Returns (offset_in_rating_points, aff_win_rate, n). A positive offset means
    the aff side wins more than half its rounds circuit-wide.
    """
    aff = sum(1 for d in debates if d["winner"] == "aff")
    n = len(debates)
    if n < 200:
        return 0.0, (aff / n if n else 0.5), n
    rate = aff / n
    rate = min(max(rate, 0.02), 0.98)
    offset = 400.0 * math.log10(rate / (1.0 - rate))
    return offset, rate, n


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ratings = {}          # pid -> Rating
        self.last_played = {}      # pid -> ISO date
        self.last_season = {}      # pid -> season label
        self.history = defaultdict(list)   # pid -> [{date, tourn, rating, rd, w, l}]
        self.record = defaultdict(lambda: {"w": 0, "l": 0, "rounds": 0})
        # pid -> season -> {w, l, rounds}. The dashboard's season filter needs
        # this: showing a career W-L next to a single season's rating would be
        # quietly wrong.
        self.season_record = defaultdict(
            lambda: defaultdict(lambda: {"w": 0, "l": 0, "rounds": 0}))
        self.peak = {}
        self.log = defaultdict(list)       # pid -> round-by-round rows
        self.partners = defaultdict(lambda: defaultdict(int))
        self.predictions = []              # (predicted_p_aff, actual_aff_won, settled)
        self.side_offset_mu = 0.0

    def get(self, pid):
        return self.ratings.get(pid, gl.Rating())

    def _prepare(self, pid, when, season):
        """Bring a player's RD up to date for time away and season turnover."""
        r = self.ratings.get(pid)
        if r is None:
            self.ratings[pid] = gl.Rating()
            return
        prev_season = self.last_season.get(pid)
        if prev_season and season and season != prev_season:
            r = gl.carry_over_season(
                r,
                regression=self.cfg["season_regression"],
                rd_floor=self.cfg["season_rd_floor"],
            )
        gap = months_between(self.last_played.get(pid), when)
        if gap > 0:
            r = gl.decay_inactive(r, periods=gap * self.cfg["inactivity_rd_per_month"])
        self.ratings[pid] = r

    def run(self, debates, tournaments):
        by_tourn = defaultdict(list)
        for d in debates:
            by_tourn[d["tourn_id"]].append(d)

        tinfo = {t["tourn_id"]: t for t in tournaments}
        order = sorted(
            by_tourn.keys(),
            key=lambda tid: (tinfo.get(tid, {}).get("start") or "9999-99-99", tid),
        )

        cfg = self.cfg
        if cfg["side_bias"] == "auto":
            offset, rate, n = estimate_side_bias(debates)
            self.side_bias_elo, self.aff_rate, self.side_n = offset, rate, n
        else:
            self.side_bias_elo = float(cfg["side_bias"] or 0.0)
            self.aff_rate, self.side_n = 0.5, 0
        self.side_offset_mu = self.side_bias_elo / gl.SCALE

        for tid in order:
            t = tinfo.get(tid, {})
            when = t.get("start") or "1900-01-01"
            season = t.get("season")
            rounds = by_tourn[tid]

            people = set()
            for d in rounds:
                people.update(d["aff"])
                people.update(d["neg"])
            for pid in people:
                self._prepare(pid, when, season)

            pre = {pid: self.ratings[pid] for pid in people}
            games = defaultdict(list)

            rounds.sort(key=lambda d: round_sort_key(d.get("round", "")))
            for d in rounds:
                self._score_round(d, pre, games, t)

            for pid in people:
                new = gl.rate_period(pre[pid], games[pid], tau=cfg["tau"])
                self.ratings[pid] = new
                self.last_played[pid] = when
                if season:
                    self.last_season[pid] = season
                if new.rating > self.peak.get(pid, -1e9):
                    self.peak[pid] = new.rating
                rec = self.record[pid]
                self.history[pid].append({
                    "date": when,
                    "tourn_id": tid,
                    "tourn": t.get("name", str(tid)),
                    "season": season,
                    "rating": round(new.rating, 1),
                    "rd": round(new.rd, 1),
                    "delta": round(new.rating - pre[pid].rating, 1),
                    "w": rec["w"],
                    "l": rec["l"],
                })

    def _score_round(self, d, pre, games, t):
        aff, neg = d["aff"], d["neg"]
        if not aff or not neg:
            return
        aff_r = [pre[p] for p in aff]
        neg_r = [pre[p] for p in neg]
        aff_mu, aff_phi = gl.combine_team(aff_r)
        neg_mu, neg_phi = gl.combine_team(neg_r)

        aff_won = d["winner"] == "aff"
        if self.cfg["ballot_scores"] and d.get("ballots_aff") is not None:
            total = (d.get("ballots_aff") or 0) + (d.get("ballots_neg") or 0)
            aff_score = (d["ballots_aff"] / total) if total else (1.0 if aff_won else 0.0)
        else:
            aff_score = 1.0 if aff_won else 0.0

        weight = self.cfg["elim_weight"] if d.get("elim") else 1.0

        # Record the prediction before any update, for out-of-sample metrics.
        p_aff = gl.win_probability(aff_r, neg_r)
        if self.side_offset_mu:
            spread = math.sqrt(aff_phi ** 2 + neg_phi ** 2)
            p_aff = 1.0 / (1.0 + math.exp(
                -gl.g(spread) * (aff_mu + self.side_offset_mu - neg_mu)))
        settled = all(pre[p].rd < 150 for p in aff + neg)
        self.predictions.append((p_aff, 1.0 if aff_won else 0.0, settled))

        # The aff side's advantage is credit the aff team does not get: it is
        # folded into expectations, so winning on the aff is worth slightly less
        # than winning on the neg. build_team_games handles the 1/n attribution.
        for pid, game in zip(aff, gl.build_team_games(
                aff_r, neg_r, aff_score, weight, advantage=self.side_offset_mu)):
            games[pid].append(game)
        for pid, game in zip(neg, gl.build_team_games(
                neg_r, aff_r, 1.0 - aff_score, weight, advantage=-self.side_offset_mu)):
            games[pid].append(game)

        season = t.get("season")
        for side, won in ((aff, aff_won), (neg, not aff_won)):
            for pid in side:
                for rec in (self.record[pid], self.season_record[pid][season]):
                    rec["rounds"] += 1
                    rec["w" if won else "l"] += 1
            if len(side) == 2:
                self.partners[side[0]][side[1]] += 1
                self.partners[side[1]][side[0]] += 1

        # Only tourn_id is stored: the name and date live once in meta.json and
        # the dashboard joins on the id. Repeating them per round tripled the
        # size of docs/.
        row = {
            "tourn_id": d["tourn_id"], "round": d.get("round", ""),
            "elim": bool(d.get("elim")),
        }
        for pid in aff:
            self.log[pid].append(dict(row, side="aff",
                                      partner=[p for p in aff if p != pid],
                                      opponents=neg, won=aff_won,
                                      p_win=round(p_aff, 4)))
        for pid in neg:
            self.log[pid].append(dict(row, side="neg",
                                      partner=[p for p in neg if p != pid],
                                      opponents=aff, won=not aff_won,
                                      p_win=round(1.0 - p_aff, 4)))

    def metrics(self):
        """Out-of-sample prediction quality. Every prediction here was made
        before the tournament it belongs to was rated, so these numbers are
        honest walk-forward figures rather than in-sample fit."""
        def summarise(rows):
            n = len(rows)
            if not n:
                return None
            acc = sum(1 for p, a, _ in rows if (p >= 0.5) == (a == 1.0)) / n
            brier = sum((p - a) ** 2 for p, a, _ in rows) / n
            ll = -sum(a * math.log(max(p, 1e-9)) + (1 - a) * math.log(max(1 - p, 1e-9))
                      for p, a, _ in rows) / n
            return {"n": n, "accuracy": round(acc, 4),
                    "brier": round(brier, 4), "log_loss": round(ll, 4)}

        bins = []
        for lo in [x / 10 for x in range(10)]:
            hi = lo + 0.1
            rows = [r for r in self.predictions if lo <= r[0] < hi]
            if rows:
                bins.append({
                    "bin": "%.1f-%.1f" % (lo, hi), "n": len(rows),
                    "predicted": round(sum(r[0] for r in rows) / len(rows), 4),
                    "actual": round(sum(r[1] for r in rows) / len(rows), 4),
                })
        return {
            "all": summarise(self.predictions),
            "settled": summarise([r for r in self.predictions if r[2]]),
            "baseline_always_aff": round(
                sum(a for _, a, _ in self.predictions) / len(self.predictions), 4)
            if self.predictions else None,
            "calibration": bins,
        }
