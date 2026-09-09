/* NDT/CEDA Glicko dashboard.
   No build step, no dependencies -- GitHub Pages serves these three files and
   the JSON that scripts/build.py emits. */
(function () {
  "use strict";

  var SCALE = 173.7178;
  var META = null, ROWS = [], BY_ID = {}, TOURN = {}, PAGE = 200;

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(x) { return (x * 100).toFixed(1) + "%"; }
  function fdate(d) {
    if (!d) return "";
    var p = d.split("-");
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+p[1] - 1]
      + " " + (+p[2]) + ", " + p[0];
  }

  /* ---------------- Glicko maths, mirroring scripts/glicko2.py -------------- */
  function gphi(phi) { return 1 / Math.sqrt(1 + 3 * phi * phi / (Math.PI * Math.PI)); }
  function team(rs) {
    var n = rs.length;
    var mu = 0, varsum = 0;
    for (var i = 0; i < n; i++) {
      mu += (rs[i].rating - 1500) / SCALE;
      varsum += Math.pow(rs[i].rd / SCALE, 2);
    }
    return { mu: mu / n, phi: Math.sqrt(varsum) / n, rating: 1500 + (mu / n) * SCALE };
  }
  // advantageMu: aff side edge, in Glicko-2 units, applied to team A.
  function winProb(a, b, advantageMu) {
    var A = team(a), B = team(b);
    var spread = Math.sqrt(A.phi * A.phi + B.phi * B.phi);
    return 1 / (1 + Math.exp(-gphi(spread) * (A.mu - B.mu + (advantageMu || 0))));
  }

  // Exposed so tests/test_js_parity.py can check these against scripts/glicko2.py.
  // The dashboard's probabilities have to agree with the engine that produced
  // the ratings, or the calculator quietly contradicts the rankings.
  window.__glicko = { team: team, winProb: winProb, gphi: gphi, SCALE: SCALE };

  /* ------------------------------ tabs ------------------------------------- */
  function showTab(name) {
    $$(".tab").forEach(function (t) { t.classList.toggle("on", t.id === "tab-" + name); });
    $$("#tabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === name); });
    if (location.hash.slice(1).split("/")[0] !== name) history.replaceState(null, "", "#" + name);
    window.scrollTo(0, 0);
  }

  /* ---------------------------- rankings ----------------------------------- */
  var sortKey = "rating", sortAsc = false, shown = PAGE;

  function seasonView(r, season) {
    if (!season) {
      return { rating: r.rating, rd: r.rd, w: r.w, l: r.l, rounds: r.rounds, tourns: r.tourns };
    }
    return r.by_season && r.by_season[season] ? r.by_season[season] : null;
  }

  function filtered() {
    var q = $("#q").value.trim().toLowerCase();
    var season = $("#season").value;
    var school = $("#school").value;
    var prov = $("#showprov").checked;
    var out = [];
    for (var i = 0; i < ROWS.length; i++) {
      var r = ROWS[i];
      var v = seasonView(r, season);
      if (!v) continue;
      if (!prov && r.provisional) continue;
      if (school && r.schools.indexOf(school) < 0) continue;
      if (q && (r.name + " " + r.schools.join(" ")).toLowerCase().indexOf(q) < 0) continue;
      out.push({ r: r, v: v });
    }
    var byFloor = $("#byfloor").checked;
    out.sort(function (x, y) {
      var a, b;
      if (sortKey === "name" || sortKey === "school") {
        a = (sortKey === "name" ? x.r.name : (x.r.school || ""));
        b = (sortKey === "name" ? y.r.name : (y.r.school || ""));
        return sortAsc ? a.localeCompare(b) : b.localeCompare(a);
      }
      if (sortKey === "rating" && byFloor) { a = x.v.rating - 2 * x.v.rd; b = y.v.rating - 2 * y.v.rd; }
      else if (sortKey === "winpct") { a = x.v.rounds ? x.v.w / x.v.rounds : 0; b = y.v.rounds ? y.v.w / y.v.rounds : 0; }
      else if (sortKey === "peak") { a = x.r.peak; b = y.r.peak; }
      else { a = x.v[sortKey]; b = y.v[sortKey]; }
      return sortAsc ? a - b : b - a;
    });
    return out;
  }

  function sparkline(pts, w, h) {
    if (!pts || pts.length < 2) return "";
    var vals = pts.map(function (p) { return p[1]; });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var span = (hi - lo) || 1;
    var d = pts.map(function (p, i) {
      var x = (i / (pts.length - 1)) * (w - 2) + 1;
      var y = h - 2 - ((p[1] - lo) / span) * (h - 4);
      return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }).join(" ");
    var up = vals[vals.length - 1] >= vals[0];
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" aria-hidden="true">'
      + '<path d="' + d + '" fill="none" stroke="' + (up ? "var(--good)" : "var(--bad)")
      + '" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  }

  function renderBoard() {
    var list = filtered();
    var body = $("#rows");
    if (!list.length) {
      body.innerHTML = '<tr><td colspan="9"><div class="empty">No debaters match those filters.</div></td></tr>';
      $("#more").hidden = true;
      $("#count").textContent = "0 debaters";
      return;
    }
    var slice = list.slice(0, shown);
    var season = $("#season").value;
    var html = slice.map(function (o, i) {
      var r = o.r, v = o.v;
      var spark = season
        ? r.spark.filter(function (p) { return p[2] === season; })
        : r.spark;
      if (spark.length < 2) spark = r.spark;
      return '<tr>'
        + '<td class="rank">' + (i + 1) + '</td>'
        + '<td><span class="who" data-id="' + esc(r.id) + '">' + esc(r.name) + '</span>'
        + (r.provisional ? '<span class="prov" title="Fewer rounds or a wider RD than the ranking threshold">prov</span>' : '')
        + '</td>'
        + '<td class="school">' + (r.school ? esc(r.school) : "&mdash;") + '</td>'
        + '<td class="num rating">' + Math.round(v.rating) + '</td>'
        + '<td class="num rd">±' + Math.round(v.rd) + '</td>'
        + '<td class="num">' + v.w + '&#8211;' + v.l + '</td>'
        + '<td class="num">' + v.rounds + '</td>'
        + '<td class="num rd">' + Math.round(r.peak) + '</td>'
        + '<td>' + sparkline(spark, 76, 22) + '</td>'
        + '</tr>';
    }).join("");
    body.innerHTML = html;
    $("#count").textContent = list.length + " debater" + (list.length === 1 ? "" : "s");
    $("#more").hidden = list.length <= shown;
  }

  /* ------------------------ debater detail sheet --------------------------- */
  function ratingChart(hist) {
    if (!hist || hist.length < 2) return '<p class="fine">Not enough tournaments yet to chart.</p>';
    var W = 800, H = 190, PL = 46, PR = 12, PT = 12, PB = 26;
    var los = hist.map(function (h) { return h.rating - h.rd; });
    var his = hist.map(function (h) { return h.rating + h.rd; });
    var lo = Math.min.apply(null, los), hi = Math.max.apply(null, his);
    var pad = (hi - lo) * 0.06 || 20;
    lo -= pad; hi += pad;
    var X = function (i) { return PL + (i / (hist.length - 1)) * (W - PL - PR); };
    var Y = function (v) { return PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB); };

    var grid = "", ticks = 4;
    for (var t = 0; t <= ticks; t++) {
      var val = lo + (hi - lo) * (t / ticks), y = Y(val);
      grid += '<line class="grid" x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1) + '"/>'
        + '<text x="' + (PL - 7) + '" y="' + (y + 3.5).toFixed(1) + '" text-anchor="end">' + Math.round(val) + '</text>';
    }
    var top = hist.map(function (h, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(h.rating + h.rd).toFixed(1); }).join(" ");
    var bot = hist.slice().reverse().map(function (h, i) {
      var j = hist.length - 1 - i;
      return "L" + X(j).toFixed(1) + " " + Y(h.rating - h.rd).toFixed(1);
    }).join(" ");
    var line = hist.map(function (h, i) { return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(h.rating).toFixed(1); }).join(" ");
    var dots = hist.map(function (h, i) {
      return '<circle class="dot" cx="' + X(i).toFixed(1) + '" cy="' + Y(h.rating).toFixed(1) + '" r="2.6">'
        + '<title>' + esc(h.tourn) + " — " + Math.round(h.rating) + " ±" + Math.round(h.rd) + '</title></circle>';
    }).join("");
    var first = '<text x="' + PL + '" y="' + (H - 8) + '">' + fdate(hist[0].date) + '</text>';
    var last = '<text x="' + (W - PR) + '" y="' + (H - 8) + '" text-anchor="end">' + fdate(hist[hist.length - 1].date) + '</text>';
    return '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img">'
      + grid + '<path class="band" d="' + top + " " + bot + ' Z"/><path class="line" d="' + line + '"/>'
      + dots + first + last + '</svg>';
  }

  function openPerson(id) {
    var r = BY_ID[id];
    if (!r) return;
    var sheet = $("#sheet"), body = $("#sheetbody");
    body.innerHTML = '<div class="empty">Loading…</div>';
    sheet.hidden = false;
    document.body.style.overflow = "hidden";
    if (location.hash.indexOf("/") < 0) history.replaceState(null, "", "#rankings/" + id);

    fetch("data/person/" + encodeURIComponent(id) + ".json")
      .then(function (r2) { return r2.ok ? r2.json() : null; })
      .catch(function () { return null; })
      .then(function (p) {
        var hist = (p && p.history) || [];
        var rounds = (p && p.rounds) || [];
        var seasons = Object.keys(r.by_season || {}).sort();

        var seasonRows = seasons.map(function (s) {
          var v = r.by_season[s];
          return '<tr><td>' + esc(s) + '</td><td class="num">' + Math.round(v.rating) + '</td>'
            + '<td class="num rd">±' + Math.round(v.rd) + '</td>'
            + '<td class="num">' + v.w + '&#8211;' + v.l + '</td>'
            + '<td class="num">' + v.tourns + '</td></tr>';
        }).join("");

        var nm = function (id) { return BY_ID[id] ? BY_ID[id].name : id; };
        var tn = function (id) { return TOURN[id] ? TOURN[id].name : ""; };
        var td = function (id) { return TOURN[id] ? TOURN[id].start : ""; };
        var logRows = rounds.slice().reverse().map(function (x) {
          var surprise = x.won && x.p_win < 0.35 ? '<span class="upset" title="Model gave this ' + pct(x.p_win) + '">upset</span>' : "";
          return '<tr><td>' + esc(fdate(td(x.tourn_id))) + '</td><td>' + esc(tn(x.tourn_id)) + '</td>'
            + '<td>' + esc(x.round) + '</td>'
            + '<td><span class="pill ' + (x.side === "aff" ? "aff" : "neg") + '">' + x.side + '</span></td>'
            + '<td>' + ((x.partner || []).length ? esc(x.partner.map(nm).join(", ")) : "&mdash;") + '</td>'
            + '<td>' + esc((x.opponents || []).map(nm).join(" & ")) + '</td>'
            + '<td class="' + (x.won ? "w" : "l") + '">' + (x.won ? "W" : "L") + surprise + '</td>'
            + '<td class="num rd">' + pct(x.p_win) + '</td></tr>';
        }).join("");

        var partners = (r.partners || []).map(function (pt) {
          var o = BY_ID[pt.id];
          return '<span class="who" data-id="' + esc(pt.id) + '">' + esc(o ? o.name : pt.id)
            + '</span> <span class="rd">(' + pt.n + ')</span>';
        }).join(" &nbsp;·&nbsp; ");

        body.innerHTML =
          '<div class="phead"><h2>' + esc(r.name) + '</h2>'
          + '<span class="sch">' + esc(r.schools.join(" / ") || "") + '</span>'
          + (r.provisional ? '<span class="prov">provisional</span>' : '') + '</div>'
          + '<div class="pstats">'
          + '<div><div class="k">Rating</div><div class="v">' + Math.round(r.rating) + '</div></div>'
          + '<div><div class="k">RD</div><div class="v">±' + Math.round(r.rd) + '</div></div>'
          + '<div><div class="k">95% range</div><div class="v">' + Math.round(r.rating - 2 * r.rd) + '&#8211;' + Math.round(r.rating + 2 * r.rd) + '</div></div>'
          + '<div><div class="k">Record</div><div class="v">' + r.w + '&#8211;' + r.l + '</div></div>'
          + '<div><div class="k">Win rate</div><div class="v">' + pct(r.winpct) + '</div></div>'
          + '<div><div class="k">Peak</div><div class="v">' + Math.round(r.peak) + '</div></div>'
          + '</div>'
          + '<div class="sublab">Rating history <span class="rd" style="text-transform:none;letter-spacing:0">(shaded band = ±1 RD)</span></div>'
          + ratingChart(hist)
          + (partners ? '<div class="sublab">Most frequent partners</div><div>' + partners + '</div>' : "")
          + (seasonRows ? '<div class="sublab">By season</div><div class="tablewrap"><table><thead><tr><th>Season</th><th class="num">Rating</th><th class="num">RD</th><th class="num">W&#8211;L</th><th class="num">Tourns</th></tr></thead><tbody>' + seasonRows + '</tbody></table></div>' : "")
          + (logRows ? '<div class="sublab">Every round</div><div class="tablewrap" style="max-height:420px;overflow-y:auto"><table><thead><tr><th>Date</th><th>Tournament</th><th>Round</th><th>Side</th><th>Partner</th><th>Opponents</th><th>Result</th><th class="num" title="Probability the model gave this debater before the tournament">Pre&nbsp;P(win)</th></tr></thead><tbody>' + logRows + '</tbody></table></div>' : "");
      });
  }

  function closeSheet() {
    $("#sheet").hidden = true;
    document.body.style.overflow = "";
    if (location.hash.indexOf("/") > -1) history.replaceState(null, "", "#" + location.hash.slice(1).split("/")[0]);
  }

  /* ------------------------------ head to head ----------------------------- */
  var picks = { A0: null, A1: null, B0: null, B1: null };

  function pickerHTML(slot) {
    var id = picks[slot];
    if (id && BY_ID[id]) {
      var r = BY_ID[id];
      return '<div class="picked"><span class="nm">' + esc(r.name) + '</span>'
        + '<span class="meta">' + Math.round(r.rating) + ' ±' + Math.round(r.rd) + '</span>'
        + '<button data-clear="' + slot + '" aria-label="Remove">&times;</button></div>';
    }
    return '<input type="text" data-slot="' + slot + '" placeholder="Search debater…" autocomplete="off" spellcheck="false">';
  }

  function renderPickers() {
    $$(".picker").forEach(function (el) { el.innerHTML = pickerHTML(el.dataset.slot); });
    renderH2H();
  }

  function slotRatings(prefix) {
    return [prefix + "0", prefix + "1"]
      .map(function (s) { return picks[s] ? BY_ID[picks[s]] : null; })
      .filter(Boolean);
  }

  function renderH2H() {
    var A = slotRatings("A"), B = slotRatings("B");
    $("#ratingA").textContent = A.length ? Math.round(team(A).rating) + " ±" + Math.round(team(A).phi * SCALE) : "";
    $("#ratingB").textContent = B.length ? Math.round(team(B).rating) + " ±" + Math.round(team(B).phi * SCALE) : "";

    var box = $("#h2hresult");
    if (A.length !== 2 || B.length !== 2) {
      box.hidden = true;
      return;
    }
    var useSide = $("#useside").checked;
    var offsetElo = (META.side_bias && META.side_bias.elo_offset) || 0;
    var affIsA = $("#affteam").value === "A";
    var adv = useSide ? (affIsA ? 1 : -1) * offsetElo / SCALE : 0;

    var p = winProb(A, B, adv);
    var tA = team(A), tB = team(B);
    var diff = tA.rating - tB.rating;
    var noSide = winProb(A, B, 0);
    var prov = A.concat(B).filter(function (r) { return r.provisional; });

    box.hidden = false;
    box.innerHTML =
      '<div class="probrow"><div><div class="lbl">Team A wins</div><div class="p">' + pct(p) + '</div></div>'
      + '<div style="text-align:right"><div class="lbl">Team B wins</div><div class="p">' + pct(1 - p) + '</div></div></div>'
      + '<div class="bar"><span class="a" style="width:' + (p * 100) + '%">' + (p > 0.14 ? pct(p) : "") + '</span>'
      + '<span class="b" style="width:' + ((1 - p) * 100) + '%">' + (p < 0.86 ? pct(1 - p) : "") + '</span></div>'
      + '<div class="barkey"><span>' + esc(A.map(function (r) { return r.name; }).join(" & ")) + '</span>'
      + '<span>' + esc(B.map(function (r) { return r.name; }).join(" & ")) + '</span></div>'
      + '<div class="detail">'
      + '<div class="d"><div class="k">Team A rating</div><div class="v">' + Math.round(tA.rating) + '</div></div>'
      + '<div class="d"><div class="k">Team B rating</div><div class="v">' + Math.round(tB.rating) + '</div></div>'
      + '<div class="d"><div class="k">Difference</div><div class="v">' + (diff >= 0 ? "+" : "") + Math.round(diff) + '</div></div>'
      + '<div class="d"><div class="k">Best of 3</div><div class="v">' + pct(p * p * (3 - 2 * p)) + '</div></div>'
      + '<div class="d"><div class="k">Ignoring sides</div><div class="v">' + pct(noSide) + '</div></div>'
      + '</div>'
      + '<div class="note">Out of 100 debates, Team A wins about <b>' + Math.round(p * 100) + '</b>.'
      + (useSide && offsetElo ? ' The ' + (affIsA ? "A" : "B") + ' side is aff, worth about '
        + Math.abs(Math.round(offsetElo)) + ' rating points on this circuit.' : "")
      + (prov.length ? ' <b>Treat this loosely:</b> ' + esc(prov.map(function (r) { return r.name; }).join(", "))
        + (prov.length > 1 ? ' have' : ' has') + ' a provisional rating, so the true probability could be materially different.' : "")
      + '</div>';
  }

  function autocomplete(input) {
    var slot = input.dataset.slot;
    var wrap = input.parentNode;
    var old = $(".ac", wrap);
    if (old) old.remove();
    var q = input.value.trim().toLowerCase();
    if (q.length < 1) return;
    var taken = Object.keys(picks).map(function (k) { return picks[k]; });
    var hits = ROWS.filter(function (r) {
      return taken.indexOf(r.id) < 0 && (r.name + " " + r.schools.join(" ")).toLowerCase().indexOf(q) > -1;
    }).slice(0, 40);
    if (!hits.length) return;
    var ac = document.createElement("div");
    ac.className = "ac";
    ac.innerHTML = hits.map(function (r, i) {
      return '<div data-pick="' + esc(r.id) + '"' + (i === 0 ? ' class="hi"' : "") + '>'
        + '<span>' + esc(r.name) + '</span><span class="s">' + esc(r.school || "") + " · "
        + Math.round(r.rating) + '</span></div>';
    }).join("");
    ac.addEventListener("mousedown", function (e) {
      var d = e.target.closest("[data-pick]");
      if (!d) return;
      e.preventDefault();
      picks[slot] = d.dataset.pick;
      renderPickers();
    });
    wrap.appendChild(ac);
  }

  /* ------------------------------- method ---------------------------------- */
  function renderMethod() {
    var m = META.metrics || {};
    var cfg = META.config || {};
    $("#m-regress").textContent = Math.round((cfg.season_regression || 0) * 100) + "%";
    $("#m-floor").textContent = Math.round(cfg.season_rd_floor || 0);
    $("#m-minrounds").textContent = cfg.min_rounds_ranked;
    $("#m-provrd").textContent = Math.round(cfg.provisional_rd || 0);
    $("#m-divs").textContent = (cfg.divisions || []).map(function (d) {
      return d.charAt(0).toUpperCase() + d.slice(1);
    }).join(", ");

    var sb = META.side_bias || {};
    $("#m-side").innerHTML = sb.n
      ? 'Across <b>' + sb.n.toLocaleString() + '</b> rated rounds the aff won <b>' + pct(sb.aff_win_rate)
        + '</b>, which is worth about <b>' + (sb.elo_offset >= 0 ? "+" : "") + Math.round(sb.elo_offset)
        + '</b> rating points to whichever team is aff.'
      : 'Not enough rounds yet to estimate side bias.';

    function card(v, k, h) {
      return '<div class="m"><div class="v">' + v + '</div><div class="k">' + k + '</div>'
        + (h ? '<div class="h">' + h + '</div>' : "") + '</div>';
    }
    var all = m.all || {}, settled = m.settled || {};
    $("#m-metrics").innerHTML =
      card(pct(all.accuracy || 0), "Accuracy, all rounds", (all.n || 0).toLocaleString() + " rounds")
      + card(pct(settled.accuracy || 0), "Accuracy, both teams settled", (settled.n || 0).toLocaleString() + " rounds")
      + card((all.brier || 0).toFixed(4), "Brier score", "lower is better; 0.25 = coin flip")
      + card((all.log_loss || 0).toFixed(4), "Log loss", "lower is better; 0.693 = coin flip");

    var maxN = Math.max.apply(null, (m.calibration || []).map(function (b) { return b.n; }).concat([1]));
    $("#calrows").innerHTML = (m.calibration || []).map(function (b) {
      return '<tr><td class="mono">' + b.bin + '</td><td class="num">' + b.n.toLocaleString() + '</td>'
        + '<td class="num">' + pct(b.predicted) + '</td><td class="num">' + pct(b.actual) + '</td>'
        + '<td><span class="calbar" style="width:' + Math.max(2, (b.n / maxN) * 150) + 'px"></span></td></tr>';
    }).join("");

    var order = ["divisions", "tau", "season_regression", "season_rd_floor",
      "inactivity_rd_per_month", "elim_weight", "ballot_scores", "side_bias",
      "min_rounds_ranked", "provisional_rd"];
    $("#cfgrows").innerHTML = order.filter(function (k) { return k in cfg; }).map(function (k) {
      var v = cfg[k];
      return '<tr><td>' + esc(k.replace(/_/g, " ")) + '</td><td>'
        + esc(Array.isArray(v) ? v.join(", ") : String(v)) + '</td></tr>';
    }).join("");

    $("#m-generated").textContent = "Ratings generated " + (META.generated || "").replace("T", " ")
      + " from " + (META.counts ? META.counts.debates.toLocaleString() : "?") + " rated rounds.";
  }

  function renderTournaments() {
    var ts = (META.tournaments || []).slice().reverse();
    $("#trows").innerHTML = ts.map(function (t) {
      return '<tr><td>' + esc(t.season || "") + '</td><td>' + esc(fdate(t.start)) + '</td>'
        + '<td>' + esc(t.name) + '</td><td class="num">' + t.debates + '</td></tr>';
    }).join("") || '<tr><td colspan="4"><div class="empty">No tournaments rated yet.</div></td></tr>';
  }

  function renderStats() {
    var c = META.counts || {};
    var m = (META.metrics && META.metrics.all) || {};
    $("#statstrip").innerHTML =
      '<div class="stat"><div class="v">' + (c.ranked || 0) + '</div><div class="k">Ranked debaters</div></div>'
      + '<div class="stat"><div class="v">' + (c.debaters || 0) + '</div><div class="k">Rated debaters</div></div>'
      + '<div class="stat"><div class="v">' + (c.debates || 0).toLocaleString() + '</div><div class="k">Rounds</div></div>'
      + '<div class="stat"><div class="v">' + (c.tournaments || 0) + '</div><div class="k">Tournaments</div></div>'
      + '<div class="stat"><div class="v">' + pct(m.accuracy || 0) + '</div><div class="k">Predictive accuracy</div></div>';
  }

  /* -------------------------------- boot ----------------------------------- */
  function wire() {
    $("#tabs").addEventListener("click", function (e) {
      if (e.target.dataset.tab) showTab(e.target.dataset.tab);
    });

    ["q", "season", "school", "showprov", "byfloor"].forEach(function (id) {
      $("#" + id).addEventListener("input", function () { shown = PAGE; renderBoard(); });
    });
    $("#morebtn").addEventListener("click", function () { shown += PAGE; renderBoard(); });

    $$("#board th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        var k = th.dataset.sort;
        if (sortKey === k) sortAsc = !sortAsc;
        else { sortKey = k; sortAsc = (k === "name" || k === "school"); }
        $$("#board th").forEach(function (o) { o.classList.remove("sorted", "asc"); });
        th.classList.add("sorted");
        if (sortAsc) th.classList.add("asc");
        renderBoard();
      });
    });

    document.addEventListener("click", function (e) {
      var who = e.target.closest(".who");
      if (who) { openPerson(who.dataset.id); return; }
      var clear = e.target.closest("[data-clear]");
      if (clear) { picks[clear.dataset.clear] = null; renderPickers(); return; }
      if (e.target.id === "sheetclose" || e.target.id === "sheet") closeSheet();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !$("#sheet").hidden) closeSheet();
    });

    document.addEventListener("input", function (e) {
      if (e.target.dataset && e.target.dataset.slot) autocomplete(e.target);
    });
    document.addEventListener("blur", function (e) {
      if (e.target.dataset && e.target.dataset.slot) {
        setTimeout(function () { var a = $(".ac", e.target.parentNode); if (a) a.remove(); }, 120);
      }
    }, true);

    ["useside", "affteam"].forEach(function (id) {
      $("#" + id).addEventListener("input", renderH2H);
    });
    $("#swap").addEventListener("click", function () {
      var t = [picks.A0, picks.A1];
      picks.A0 = picks.B0; picks.A1 = picks.B1;
      picks.B0 = t[0]; picks.B1 = t[1];
      renderPickers();
    });
    $("#randomize").addEventListener("click", function () {
      var pool = ROWS.filter(function (r) { return !r.provisional; });
      if (pool.length < 4) pool = ROWS.slice();
      var seen = {};
      ["A0", "A1", "B0", "B1"].forEach(function (s) {
        var pick;
        do { pick = pool[Math.floor(Math.random() * pool.length)]; } while (pick && seen[pick.id]);
        if (pick) { seen[pick.id] = 1; picks[s] = pick.id; }
      });
      renderPickers();
    });
  }

  function boot(meta, rows) {
    META = meta;
    ROWS = rows;
    rows.forEach(function (r) { BY_ID[r.id] = r; });
    (meta.tournaments || []).forEach(function (t) { TOURN[t.tourn_id] = t; });

    if (meta.synthetic) {
      var b = $("#banner");
      b.hidden = false;
      b.innerHTML = "<b>Sample data.</b> These ratings come from a simulated circuit, not from real "
        + "results &mdash; the pipeline is wired up and validated, but Tabroom results have not been "
        + "loaded yet. Names and numbers here are not real people.";
    }

    var seasons = (meta.seasons || []).slice().reverse();
    $("#season").innerHTML = '<option value="">All seasons (current rating)</option>'
      + seasons.map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join("");

    var schools = {};
    rows.forEach(function (r) { (r.schools || []).forEach(function (s) { if (s) schools[s] = 1; }); });
    $("#school").innerHTML = '<option value="">All schools</option>'
      + Object.keys(schools).sort().map(function (s) { return '<option>' + esc(s) + '</option>'; }).join("");

    renderStats();
    renderBoard();
    renderTournaments();
    renderMethod();
    renderPickers();

    var parts = location.hash.slice(1).split("/");
    showTab(["rankings", "h2h", "tournaments", "method"].indexOf(parts[0]) > -1 ? parts[0] : "rankings");
    if (parts[1]) openPerson(parts[1]);
  }

  wire();
  Promise.all([
    fetch("data/meta.json").then(function (r) { return r.json(); }),
    fetch("data/ratings.json").then(function (r) { return r.json(); })
  ]).then(function (res) { boot(res[0], res[1]); })
    .catch(function (err) {
      $("#main").innerHTML = '<div class="empty">Could not load rating data.<br><br>'
        + 'Run <span class="mono">python scripts/build.py</span> to generate '
        + '<span class="mono">docs/data/</span>, then reload.<br><br>'
        + '<span class="fine">' + esc(err) + '</span></div>';
    });
})();
