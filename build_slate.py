"""
build_slate.py — assembles every model input from free public APIs and
writes data/slate.json for the static page to read.

Sources (all free, no keys):
  MLB StatsAPI   schedule, probable starters, standings, pitcher lines,
                 team platoon splits, relief innings for fatigue
  Kalshi         KXMLBGAME moneyline prices (public, unauthenticated)

Nothing here needs credentials. Run it locally or on a cron:
    python build_slate.py --days 2

Every fetch degrades: if a source fails, the field is left null and the
model falls back to its own default. `warnings` in the output records
exactly what could not be filled, so a half-broken slate is visible
rather than silently wrong.
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

STATS = "https://statsapi.mlb.com/api/v1"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXMLBGAME"
UA = {"User-Agent": "mlb-model-v4/1.0 (personal research)"}
TIMEOUT = 30

# wOBA linear weights — FanGraphs' recent-era values. Update yearly.
W = {"bb": 0.690, "hbp": 0.720, "1b": 0.880, "2b": 1.271, "3b": 1.616, "hr": 2.101}

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print("  ! " + msg, file=sys.stderr)


def get(url, **params):
    r = requests.get(url, params=params or None, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def ip_to_float(ip):
    """MLB writes 5.2 innings meaning five and two thirds."""
    whole, _, frac = str(ip or "0").partition(".")
    try:
        return int(whole) + int(frac or 0) / 3
    except ValueError:
        return 0.0


# ————————————————————————————————————————————————
# Schedule and probable starters
# ————————————————————————————————————————————————
def fetch_schedule(days):
    start = date.today()
    end = start + timedelta(days=days - 1)
    data = get(f"{STATS}/schedule", sportId=1,
               startDate=start.isoformat(), endDate=end.isoformat(),
               hydrate="probablePitcher,team,linescore")
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            state = g.get("status", {}).get("abstractGameState")
            if state == "Final":
                continue
            home, away = g["teams"]["home"], g["teams"]["away"]
            games.append({
                "gamePk": g["gamePk"],
                "date": d["date"],
                "startsAt": g.get("gameDate"),
                "state": state,
                "venue": g.get("venue", {}).get("name"),
                "home": {"id": home["team"]["id"], "name": home["team"]["name"],
                         "probable": (home.get("probablePitcher") or {}).get("id"),
                         "probableName": (home.get("probablePitcher") or {}).get("fullName")},
                "away": {"id": away["team"]["id"], "name": away["team"]["name"],
                         "probable": (away.get("probablePitcher") or {}).get("id"),
                         "probableName": (away.get("probablePitcher") or {}).get("fullName")},
            })
    return games


# ————————————————————————————————————————————————
# Records
# ————————————————————————————————————————————————
def fetch_records(season):
    out = {}
    try:
        data = get(f"{STATS}/standings", leagueId="103,104", season=season,
                   standingsTypes="regularSeason")
    except Exception as e:
        warn(f"standings unavailable ({e}); records fall back to .500")
        return out
    for rec in data.get("records", []):
        for t in rec.get("teamRecords", []):
            out[t["team"]["id"]] = {"wins": t.get("wins", 0), "losses": t.get("losses", 0)}
    return out


# ————————————————————————————————————————————————
# Pitching: one bulk call, then FIP for every arm
# ————————————————————————————————————————————————
def fip_components(s):
    return (ip_to_float(s.get("inningsPitched")),
            float(s.get("homeRuns") or 0),
            float(s.get("baseOnBalls") or 0),
            float(s.get("hitByPitch") or 0),
            float(s.get("strikeOuts") or 0))


def raw_fip(ip, hr, bb, hbp, k):
    if ip <= 0:
        return None
    return (13 * hr + 3 * (bb + hbp) - 2 * k) / ip


def fetch_pitching(season):
    """Returns (per_pitcher, bullpen_by_team, league) with FIP already
    calibrated so that league FIP equals league ERA."""
    try:
        data = get(f"{STATS}/stats", stats="season", group="pitching",
                   season=season, sportId=1, playerPool="All",
                   gameType="R", limit=2000)
    except Exception as e:
        warn(f"bulk pitching stats unavailable ({e}); starters fall back to league rate")
        return {}, {}, {}

    splits = []
    for blob in data.get("stats", []):
        splits.extend(blob.get("splits", []))
    if not splits:
        warn("bulk pitching returned no splits")
        return {}, {}, {}

    tot = [0.0] * 5
    tot_er = 0.0
    people = {}
    pen = defaultdict(lambda: [0.0] * 5)

    for sp in splits:
        s = sp.get("stat", {})
        pid = (sp.get("player") or {}).get("id")
        tid = (sp.get("team") or {}).get("id")
        if pid is None:
            continue
        ip, hr, bb, hbp, k = fip_components(s)
        if ip <= 0:
            continue
        gs = float(s.get("gamesStarted") or 0)
        g = float(s.get("gamesPlayed") or 0) or 1.0

        for i, v in enumerate((ip, hr, bb, hbp, k)):
            tot[i] += v
        tot_er += float(s.get("earnedRuns") or 0)

        people[pid] = {"teamId": tid, "ip": ip, "gs": gs, "g": g,
                       "rawFip": raw_fip(ip, hr, bb, hbp, k),
                       "era": float(s.get("era") or 0) if s.get("era") not in (None, "-.--") else None,
                       "ipPerStart": (ip / gs) if gs > 0 else None}
        # A reliever is anyone who mostly does not start.
        if tid is not None and gs / g < 0.5:
            for i, v in enumerate((ip, hr, bb, hbp, k)):
                pen[tid][i] += v

    lg_ip = tot[0]
    lg_era = (tot_er * 9 / lg_ip) if lg_ip else 4.10
    lg_raw = raw_fip(*tot) or 0.0
    c_fip = lg_era - lg_raw  # the FIP constant, derived rather than assumed

    for p in people.values():
        p["fip"] = round(p["rawFip"] + c_fip, 3) if p["rawFip"] is not None else None

    bullpen = {}
    for tid, c in pen.items():
        rf = raw_fip(*c)
        if rf is not None:
            bullpen[tid] = {"fip": round(rf + c_fip, 3), "ip": round(c[0], 1)}

    league = {"era": round(lg_era, 3), "fipConstant": round(c_fip, 3),
              "ip": round(lg_ip, 1)}
    print(f"  league ERA {lg_era:.2f}, FIP constant {c_fip:.2f}")
    return people, bullpen, league


def fetch_handedness(pitcher_ids):
    hands = {}
    ids = [str(p) for p in pitcher_ids if p]
    if not ids:
        return hands
    try:
        data = get(f"{STATS}/people", personIds=",".join(ids))
        for p in data.get("people", []):
            hands[p["id"]] = (p.get("pitchHand") or {}).get("code")
    except Exception as e:
        warn(f"pitcher handedness unavailable ({e}); platoon term skipped")
    return hands


# ————————————————————————————————————————————————
# Platoon: team wOBA vs LHP and vs RHP, computed from components
# ————————————————————————————————————————————————
def woba_from(s):
    ab = float(s.get("atBats") or 0)
    h = float(s.get("hits") or 0)
    d = float(s.get("doubles") or 0)
    t = float(s.get("triples") or 0)
    hr = float(s.get("homeRuns") or 0)
    bb = float(s.get("baseOnBalls") or 0)
    ibb = float(s.get("intentionalWalks") or 0)
    hbp = float(s.get("hitByPitch") or 0)
    sf = float(s.get("sacFlies") or 0)
    singles = h - d - t - hr
    denom = ab + bb - ibb + sf + hbp
    if denom <= 0:
        return None
    num = (W["bb"] * (bb - ibb) + W["hbp"] * hbp + W["1b"] * singles +
           W["2b"] * d + W["3b"] * t + W["hr"] * hr)
    return round(num / denom, 4)


def fetch_platoon(team_ids, season):
    """{teamId: {overall, vL, vR}} — wOBA built from raw counting stats."""
    out = {}
    for tid in sorted(team_ids):
        entry = {}
        try:
            season_stats = get(f"{STATS}/teams/{tid}/stats", stats="season",
                               group="hitting", season=season, gameType="R")
            for blob in season_stats.get("stats", []):
                for sp in blob.get("splits", []):
                    entry["overall"] = woba_from(sp.get("stat", {}))
        except Exception as e:
            warn(f"team {tid} season hitting unavailable ({e})")

        try:
            sp_stats = get(f"{STATS}/teams/{tid}/stats", stats="statSplits",
                           group="hitting", sitCodes="vl,vr", season=season,
                           gameType="R")
            for blob in sp_stats.get("stats", []):
                for sp in blob.get("splits", []):
                    code = (sp.get("split") or {}).get("code")
                    w = woba_from(sp.get("stat", {}))
                    if code == "vl":
                        entry["vL"] = w
                    elif code == "vr":
                        entry["vR"] = w
        except Exception as e:
            warn(f"team {tid} platoon splits unavailable ({e})")

        if entry:
            out[tid] = entry
    return out


# ————————————————————————————————————————————————
# Bullpen fatigue
# ————————————————————————————————————————————————
def fetch_fatigue(days=3):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    totals = defaultdict(float)
    try:
        sched = get(f"{STATS}/schedule", sportId=1,
                    startDate=start.isoformat(), endDate=end.isoformat())
    except Exception as e:
        warn(f"fatigue schedule unavailable ({e})")
        return {}
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            try:
                box = get(f"{STATS}/game/{g['gamePk']}/boxscore")
            except Exception:
                continue
            for side in ("home", "away"):
                t = box["teams"][side]
                tid = t["team"]["id"]
                for pid in t.get("pitchers", [])[1:]:   # skip the starter
                    st = t["players"].get(f"ID{pid}", {})
                    p = st.get("stats", {}).get("pitching", {})
                    totals[tid] += ip_to_float(p.get("inningsPitched"))
    return {k: round(v, 1) for k, v in totals.items()}


# ————————————————————————————————————————————————
# Kalshi
# ————————————————————————————————————————————————
ABBR = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT", 135: "SD", 136: "SEA",
    137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def fetch_kalshi():
    """All open KXMLBGAME markets. Returns a list of light dicts."""
    markets, cursor = [], None
    try:
        while True:
            params = {"series_ticker": SERIES, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = get(f"{KALSHI}/markets", **params)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                break
    except Exception as e:
        warn(f"Kalshi unavailable ({e}); slate will have no market prices")
        return []

    out = []
    for m in markets:
        yes_bid, yes_ask = m.get("yes_bid"), m.get("yes_ask")
        mid = None
        if yes_bid is not None and yes_ask is not None and yes_ask > 0:
            mid = (yes_bid + yes_ask) / 200
        out.append({
            "ticker": m.get("ticker", ""),
            "eventTicker": m.get("event_ticker", ""),
            "title": m.get("title", ""),
            "subtitle": m.get("yes_sub_title") or m.get("subtitle") or "",
            "yesBid": yes_bid, "yesAsk": yes_ask,
            "last": m.get("last_price"),
            "mid": round(mid, 4) if mid is not None else None,
            "volume": m.get("volume"),
            "closeTime": m.get("close_time"),
        })
    print(f"  Kalshi: {len(out)} open {SERIES} markets")
    return out


def match_kalshi(game, markets):
    """Kalshi's ticker format is not contractual, so match on content:
    a market belongs to this game if both team abbreviations appear in the
    ticker, and the YES side names one of the two teams."""
    h, a = ABBR.get(game["home"]["id"]), ABBR.get(game["away"]["id"])
    if not h or not a:
        return None
    hits = []
    for m in markets:
        blob = (m["ticker"] + " " + m["title"] + " " + m["subtitle"]).upper()
        tick = m["ticker"].upper()
        if h in tick and a in tick:
            hits.append((m, blob))
    if not hits:
        return None

    home_nick = game["home"]["name"].split()[-1].upper()
    away_nick = game["away"]["name"].split()[-1].upper()
    for m, blob in hits:
        tail = m["ticker"].upper().rsplit("-", 1)[-1]
        if tail == h or home_nick in (m["subtitle"] or "").upper():
            return {"side": "home", **m}
        if tail == a or away_nick in (m["subtitle"] or "").upper():
            return {"side": "away", **m}
    m = hits[0][0]
    warn(f"Kalshi market {m['ticker']} matched {a}@{h} but side is ambiguous")
    return {"side": "unknown", **m}


# ————————————————————————————————————————————————
# Assemble
# ————————————————————————————————————————————————
def build(days):
    season = date.today().year
    print(f"season {season}, {days} day(s) ahead")

    print("· schedule")
    games = fetch_schedule(days)
    if not games:
        warn("no upcoming games found")
    print(f"  {len(games)} games")

    print("· records")
    records = fetch_records(season)

    print("· pitching")
    people, bullpen, league = fetch_pitching(season)

    print("· handedness")
    probables = [g[s]["probable"] for g in games for s in ("home", "away")]
    hands = fetch_handedness(set(probables))

    print("· platoon splits")
    team_ids = {g[s]["id"] for g in games for s in ("home", "away")}
    platoon = fetch_platoon(team_ids, season)

    print("· bullpen fatigue")
    fatigue = fetch_fatigue(3)

    print("· kalshi")
    markets = fetch_kalshi()

    def side(g, which):
        s = g[which]
        tid = s["id"]
        rec = records.get(tid, {})
        arm = people.get(s["probable"]) or {}
        pen = bullpen.get(tid) or {}
        # The opposing starter's hand drives which platoon split applies.
        other = "away" if which == "home" else "home"
        opp_hand = hands.get(g[other]["probable"])
        pl = platoon.get(tid, {})
        vs_hand = pl.get("vL") if opp_hand == "L" else pl.get("vR") if opp_hand == "R" else None
        return {
            "id": tid,
            "name": s["name"],
            "abbr": ABBR.get(tid),
            "wins": rec.get("wins"),
            "losses": rec.get("losses"),
            "starter": s.get("probableName"),
            "starterId": s.get("probable"),
            "starterHand": hands.get(s["probable"]),
            "sp_rate": arm.get("fip"),
            "sp_era": arm.get("era"),
            "sp_ip": round(arm["ipPerStart"], 2) if arm.get("ipPerStart") else None,
            "bp_rate": pen.get("fip"),
            "bp_season_ip": pen.get("ip"),
            "bp_ip3": fatigue.get(tid),
            "woba_vs_hand": vs_hand,
            "woba_overall": pl.get("overall"),
            "oppHand": opp_hand,
            "ptype_rv100": None,
        }

    slate = []
    for g in games:
        entry = {
            "gamePk": g["gamePk"], "date": g["date"], "startsAt": g["startsAt"],
            "state": g["state"], "venue": g["venue"],
            "home": side(g, "home"), "away": side(g, "away"),
            "kalshi": match_kalshi(g, markets),
        }
        slate.append(entry)

    matched = sum(1 for s in slate if s["kalshi"])
    print(f"  Kalshi matched {matched}/{len(slate)} games")

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "season": season,
        "league": league,
        "wobaWeights": W,
        "games": slate,
        "warnings": WARNINGS,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="days of schedule ahead")
    ap.add_argument("--out", default="data/slate.json")
    args = ap.parse_args()

    payload = build(args.days)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    print(f"wrote {out} — {len(payload['games'])} games, "
          f"{len(payload['warnings'])} warnings")


if __name__ == "__main__":
    main()
