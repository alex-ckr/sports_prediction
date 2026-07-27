/* model.js — v4 game-outcome model, ported line-for-line from mlb_model_v4.py.
   No dependencies. Runs in a browser or in Node. */

(function (root) {
  "use strict";

  // ——— Constants (editable at runtime from the Constants panel) ———
  var K = {
    REGRESSION_GAMES: 70,      // ballast toward .500 for team records
    LEAGUE_RATE: 4.10,         // league-average ERA ~ FIP
    RUNS_PER_WIN: 10.0,
    HFA_ODDS: 1.19,            // ~54% home edge for even teams
    LOGODDS_PER_WIN: 4.0,      // dp/dlogit ~= 0.25 at p = 0.5
    DEFAULT_SP_IP: 5.5,
    FATIGUE_FREE_IP3: 9.0,     // relief IP over 3 days with no penalty
    FATIGUE_RUNS_PER_IP: 0.12, // extra runs/9 per relief IP beyond that
    WOBA_TO_RUNS: 1.15,
    PA_VS_STARTER: 26,
    PITCHES_VS_STARTER: 90
  };
  var DEFAULT_K = Object.assign({}, K);

  var SLOT_PA = [4.7, 4.6, 4.5, 4.4, 4.3, 4.2, 4.1, 4.0, 3.9];

  // ——— Core model ———
  function teamDay(o) {
    return {
      name: o.name || "",
      wins: num(o.wins, 0),
      losses: num(o.losses, 0),
      starter: o.starter || "",
      sp_rate: num(o.sp_rate, K.LEAGUE_RATE),
      bp_rate: nullable(o.bp_rate) === null ? K.LEAGUE_RATE : num(o.bp_rate),
      sp_ip: nullable(o.sp_ip) === null ? K.DEFAULT_SP_IP : num(o.sp_ip),
      bp_ip3: nullable(o.bp_ip3),
      woba_vs_hand: nullable(o.woba_vs_hand),
      woba_overall: nullable(o.woba_overall),
      ptype_rv100: nullable(o.ptype_rv100) === null ? 0.0 : num(o.ptype_rv100)
    };
  }

  function num(v, fallback) {
    var x = typeof v === "number" ? v : parseFloat(v);
    return isFinite(x) ? x : (fallback === undefined ? 0 : fallback);
  }

  function nullable(v) {
    if (v === null || v === undefined || v === "") return null;
    var x = typeof v === "number" ? v : parseFloat(v);
    return isFinite(x) ? x : null;
  }

  function regressedWpct(w, l) {
    var g = w + l;
    return (w + 0.5 * K.REGRESSION_GAMES) / (g + K.REGRESSION_GAMES);
  }

  /** P(team with true-talent win% a beats team with b). */
  function log5(a, b) {
    return (a * (1 - b)) / (a * (1 - b) + b * (1 - a));
  }

  function effectiveBpRate(t) {
    var rate = t.bp_rate;
    if (t.bp_ip3 !== null && t.bp_ip3 > K.FATIGUE_FREE_IP3) {
      rate += (t.bp_ip3 - K.FATIGUE_FREE_IP3) * K.FATIGUE_RUNS_PER_IP;
    }
    return rate;
  }

  function pitchingRunsVsAvg(t) {
    var spIp = Math.min(Math.max(t.sp_ip, 0.0), 9.0);
    return ((t.sp_rate - K.LEAGUE_RATE) * spIp +
            (effectiveBpRate(t) - K.LEAGUE_RATE) * (9 - spIp)) / 9;
  }

  /** Runs the lineup adds vs the opposing starter beyond its overall
      quality (which W-L already prices in). */
  function offenseRunsVsAvg(t) {
    var runs = 0.0;
    if (t.woba_vs_hand !== null && t.woba_overall !== null) {
      runs += (t.woba_vs_hand - t.woba_overall) / K.WOBA_TO_RUNS * K.PA_VS_STARTER;
    }
    runs += t.ptype_rv100 * K.PITCHES_VS_STARTER / 100;
    return runs;
  }

  /** Home win probability, plus every intermediate term for display. */
  function predict(home, away) {
    var hTalent = regressedWpct(home.wins, home.losses);
    var aTalent = regressedWpct(away.wins, away.losses);
    var p0 = log5(hTalent, aTalent);

    var baseLogit = Math.log(p0 / (1 - p0));
    var hfaLogit = Math.log(K.HFA_ODDS);

    var hPitch = pitchingRunsVsAvg(home);
    var aPitch = pitchingRunsVsAvg(away);
    var hOff = offenseRunsVsAvg(home);
    var aOff = offenseRunsVsAvg(away);

    var pitchDelta = aPitch - hPitch;     // runs, home's favor
    var offDelta = hOff - aOff;           // runs, home's favor
    var delta = pitchDelta + offDelta;
    var deltaLogit = (delta / K.RUNS_PER_WIN) * K.LOGODDS_PER_WIN;

    var logit = baseLogit + hfaLogit + deltaLogit;
    var p = 1 / (1 + Math.exp(-logit));

    return {
      p: p,
      homeTalent: hTalent,
      awayTalent: aTalent,
      p0: p0,
      baseLogit: baseLogit,
      hfaLogit: hfaLogit,
      homePitch: hPitch,
      awayPitch: aPitch,
      homeOff: hOff,
      awayOff: aOff,
      homeBpEff: effectiveBpRate(home),
      awayBpEff: effectiveBpRate(away),
      pitchDelta: pitchDelta,
      offDelta: offDelta,
      runDelta: delta,
      deltaLogit: deltaLogit,
      logit: logit
    };
  }

  function report(home, away) {
    var p = predict(home, away).p;
    var fav = p >= 0.5 ? home : away;
    var prob = p >= 0.5 ? p : 1 - p;
    return away.name + " @ " + home.name + " -> " + fav.name + " " +
           (prob * 100).toFixed(1) + "% (home " + (p * 100).toFixed(1) + "%)";
  }

  // ——— Platoon helper ———
  /** Nine hitters' wOBA vs the relevant hand, in batting order,
      weighted by how often each lineup slot bats. */
  function lineupWoba(playerWobas) {
    var w = SLOT_PA.slice(0, playerWobas.length);
    var top = 0, bot = 0;
    for (var i = 0; i < w.length; i++) {
      top += playerWobas[i] * w[i];
      bot += w[i];
    }
    return bot ? top / bot : 0;
  }

  // ——— Pitch-type matchup ———
  /** mix: [{type, usage}] where usage sums to ~1.0.
      rv: {pitchType: run value per 100 pitches for this lineup}.
      Positive = this lineup profiles well against this arsenal. */
  function matchupRv100(mix, rv) {
    var total = 0;
    for (var i = 0; i < mix.length; i++) {
      var u = num(mix[i].usage, 0);
      var v = num(rv[mix[i].type], 0);
      total += u * v;
    }
    return total;
  }

  // ——— Fair odds from a probability (American) ———
  function americanOdds(p) {
    if (!(p > 0 && p < 1)) return "—";
    var o = p >= 0.5 ? -100 * p / (1 - p) : 100 * (1 - p) / p;
    return (o > 0 ? "+" : "") + Math.round(o);
  }

  // ——— Calibration ———
  function calibration(rows) {
    var done = rows.filter(function (r) { return r.won === "1" || r.won === "0"; });
    if (!done.length) return null;
    var probs = done.map(function (r) { return parseFloat(r.prob); });
    var outs = done.map(function (r) { return parseInt(r.won, 10); });
    var n = done.length;
    var brier = 0, wins = 0, sump = 0;
    for (var i = 0; i < n; i++) {
      brier += Math.pow(probs[i] - outs[i], 2);
      wins += outs[i];
      sump += probs[i];
    }
    var buckets = [];
    [0.5, 0.6, 0.7, 0.8, 0.9].forEach(function (lo) {
      var b = [];
      for (var i = 0; i < n; i++) {
        if (probs[i] >= lo && probs[i] < lo + 0.1) b.push([probs[i], outs[i]]);
      }
      if (b.length) {
        buckets.push({
          lo: lo,
          predicted: b.reduce(function (s, x) { return s + x[0]; }, 0) / b.length,
          actual: b.reduce(function (s, x) { return s + x[1]; }, 0) / b.length,
          n: b.length
        });
      }
    });
    return {
      n: n,
      winRate: wins / n,
      avgPredicted: sump / n,
      brier: brier / n,
      buckets: buckets
    };
  }

  // ——— Bullpen fatigue, straight from MLB StatsAPI in the browser ———
  var API = "https://statsapi.mlb.com/api/v1";

  function ymd(d) {
    return d.getFullYear() + "-" +
           String(d.getMonth() + 1).padStart(2, "0") + "-" +
           String(d.getDate()).padStart(2, "0");
  }

  function ipToFloat(ip) {
    var parts = String(ip || "0").split(".");
    return (parseInt(parts[0], 10) || 0) + (parseInt(parts[1], 10) || 0) / 3;
  }

  /** Resolves to {teamFullName: relief IP over the last `days` days}. */
  function reliefIpLastDays(days, onProgress) {
    days = days || 3;
    var end = new Date();
    end.setDate(end.getDate() - 1);
    var start = new Date(end);
    start.setDate(start.getDate() - (days - 1));

    var url = API + "/schedule?sportId=1&startDate=" + ymd(start) +
              "&endDate=" + ymd(end);

    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("Schedule request failed (" + r.status + ")");
      return r.json();
    }).then(function (sched) {
      var pks = [];
      (sched.dates || []).forEach(function (d) {
        (d.games || []).forEach(function (g) {
          if (g.status && g.status.abstractGameState === "Final") pks.push(g.gamePk);
        });
      });
      var totals = {};
      var done = 0;
      if (onProgress) onProgress(0, pks.length);

      function one(pk) {
        return fetch(API + "/game/" + pk + "/boxscore")
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (box) {
            if (box) {
              ["home", "away"].forEach(function (side) {
                var t = box.teams[side];
                var team = t.team.name;
                var arms = (t.pitchers || []).slice(1); // skip the starter
                arms.forEach(function (pid) {
                  var st = t.players["ID" + pid];
                  if (!st || !st.stats || !st.stats.pitching) return;
                  totals[team] = (totals[team] || 0) +
                                 ipToFloat(st.stats.pitching.inningsPitched);
                });
              });
            }
          })
          .catch(function () { /* one bad box score shouldn't sink the pull */ })
          .then(function () {
            done++;
            if (onProgress) onProgress(done, pks.length);
          });
      }

      // Six at a time — polite to the API, still fast.
      var queue = pks.slice();
      function worker() {
        if (!queue.length) return Promise.resolve();
        return one(queue.shift()).then(worker);
      }
      var workers = [];
      for (var i = 0; i < 6; i++) workers.push(worker());
      return Promise.all(workers).then(function () {
        return { totals: totals, start: ymd(start), end: ymd(end), games: pks.length };
      });
    });
  }

  // ——— Slate: turn a build_slate.py record into two TeamDays ———
  /** Missing fields stay null so teamDay falls back to its own defaults. */
  function fromSlate(game) {
    function one(s) {
      return teamDay({
        name: s.name, wins: s.wins, losses: s.losses,
        starter: s.starter || "TBD",
        sp_rate: s.sp_rate === null || s.sp_rate === undefined ? K.LEAGUE_RATE : s.sp_rate,
        bp_rate: s.bp_rate, sp_ip: s.sp_ip, bp_ip3: s.bp_ip3,
        woba_vs_hand: s.woba_vs_hand, woba_overall: s.woba_overall,
        ptype_rv100: s.ptype_rv100
      });
    }
    return { home: one(game.home), away: one(game.away) };
  }

  /** Which model inputs were actually filled by the feed. */
  function coverage(game) {
    var need = ["wins", "sp_rate", "bp_rate", "sp_ip", "bp_ip3", "woba_vs_hand"];
    var have = 0, missing = [];
    need.forEach(function (f) {
      var h = game.home[f] !== null && game.home[f] !== undefined;
      var a = game.away[f] !== null && game.away[f] !== undefined;
      if (h && a) have++; else missing.push(f);
    });
    return { have: have, total: need.length, missing: missing };
  }

  // ——— Market comparison ———
  /** Kalshi's trading fee: 0.07 × contracts × price × (1 − price), rounded
      up to the cent. Quoted and returned in dollars per single contract. */
  function kalshiFee(price) {
    return Math.ceil(0.07 * price * (1 - price) * 100) / 100;
  }

  /** Expected value of one contract, net of fee, buying at `ask` when the
      model says the event happens with probability `p`. Contracts settle at
      $1, so EV is in dollars per contract.

      `breakEven` is the probability at which this trade stops losing money —
      the ask plus the fee. `netEdge` is how far past that the model sits, and
      it is the only edge number worth ranking on: raw model-minus-mid ignores
      that you cross the spread and pay a fee to get filled. */
  function contractEv(p, ask) {
    if (!(ask > 0 && ask < 1)) return null;
    var fee = kalshiFee(ask);
    var breakEven = ask + fee;
    return {
      ask: ask, fee: fee, cost: breakEven, breakEven: breakEven,
      ev: p - breakEven, roi: (p - breakEven) / breakEven,
      netEdge: p - breakEven
    };
  }

  /** Compare a model home-win probability against a Kalshi market.
      `k` is the slate's kalshi block; prices arrive in cents. */
  function compareMarket(p, k) {
    if (!k) return null;
    var yesIsHome = k.side === "home";
    if (k.side !== "home" && k.side !== "away") return { unknownSide: true, market: k };

    var pYes = yesIsHome ? p : 1 - p;           // model prob of the YES side
    var mid = k.mid;                             // dollars
    var ask = k.yesAsk !== null && k.yesAsk !== undefined ? k.yesAsk / 100 : null;
    var bid = k.yesBid !== null && k.yesBid !== undefined ? k.yesBid / 100 : null;

    var yes = ask !== null ? contractEv(pYes, ask) : null;
    // Buying NO costs one minus the YES bid, and wins when YES loses.
    var no = bid !== null ? contractEv(1 - pYes, 1 - bid) : null;

    var best = null;
    if (yes && no) best = yes.netEdge >= no.netEdge ? "yes" : "no";
    else if (yes) best = "yes";
    else if (no) best = "no";
    var bestLeg = best === "yes" ? yes : best === "no" ? no : null;

    return {
      market: k,
      yesIsHome: yesIsHome,
      modelYes: pYes,
      marketYes: mid,
      // Raw disagreement with the mid — what it looks like before costs.
      edge: mid !== null && mid !== undefined ? pYes - mid : null,
      // What you must clear to profit, in points above the mid.
      hurdle: (bestLeg && mid !== null && mid !== undefined)
        ? bestLeg.breakEven - (best === "yes" ? mid : 1 - mid) : null,
      yes: yes, no: no, best: best, bestLeg: bestLeg,
      netEdge: bestLeg ? bestLeg.netEdge : null
    };
  }

  root.Model = {
    K: K,
    fromSlate: fromSlate,
    coverage: coverage,
    kalshiFee: kalshiFee,
    contractEv: contractEv,
    compareMarket: compareMarket,
    DEFAULT_K: DEFAULT_K,
    SLOT_PA: SLOT_PA,
    teamDay: teamDay,
    regressedWpct: regressedWpct,
    log5: log5,
    effectiveBpRate: effectiveBpRate,
    pitchingRunsVsAvg: pitchingRunsVsAvg,
    offenseRunsVsAvg: offenseRunsVsAvg,
    predict: predict,
    report: report,
    lineupWoba: lineupWoba,
    matchupRv100: matchupRv100,
    americanOdds: americanOdds,
    calibration: calibration,
    reliefIpLastDays: reliefIpLastDays,
    ipToFloat: ipToFloat
  };
})(typeof window !== "undefined" ? window : globalThis);
