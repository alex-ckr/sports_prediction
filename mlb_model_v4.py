"""
mlb_model_v4.py — Ben's complete MLB game-outcome model, one file.

Everything lives here: the v4 prediction engine, the platoon and
pitch-type helpers, the bullpen-fatigue puller, and the pick logger.

MODEL (v4) = team strength (regressed win% -> log5)
           + home-field advantage
           + pitching: starter FIP/ERA x expected IP, bullpen rate over
             the remaining innings, with a 3-day fatigue penalty
           + offense vs starter: platoon wOBA gap + pitch-type matchup
           ... all pitching/offense terms applied in log-odds space.

Only the standard library is required to predict. `requests` is needed
for the fatigue puller and `pandas` for the CSV helpers (both optional).

Quick start:
    from mlb_model_v4 import TeamDay, predict, report
    home = TeamDay("Phillies", 62, 43, "Sanchez", 2.71, bp_rate=4.40, sp_ip=6.0)
    away = TeamDay("Yankees", 61, 44, "Warren", 4.00, bp_rate=3.01, sp_ip=4.5)
    print(report(home, away))
"""
import csv
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# ————————————————————————————————————————————————
# Constants
# ————————————————————————————————————————————————
REGRESSION_GAMES = 70        # ballast toward .500 for team records
LEAGUE_RATE = 4.10           # league-average ERA ~ FIP
RUNS_PER_WIN = 10.0
HFA_ODDS = 1.19              # ~54% home edge for even teams
LOGODDS_PER_WIN = 4.0        # dp/dlogit ~= 0.25 at p = 0.5
DEFAULT_SP_IP = 5.5
FATIGUE_FREE_IP3 = 9.0       # relief IP over 3 days with no penalty
FATIGUE_RUNS_PER_IP = 0.12   # extra runs/9 per relief IP beyond that
WOBA_TO_RUNS = 1.15          # standard wOBA-to-runs scale
PA_VS_STARTER = 26           # lineup PA vs a typical starter
PITCHES_VS_STARTER = 90


# ————————————————————————————————————————————————
# Core model
# ————————————————————————————————————————————————
@dataclass
class TeamDay:
    """One team's inputs for one game.

    sp_rate / bp_rate : starter and bullpen FIP (preferred) or ERA
    sp_ip             : expected starter innings THIS game (opener ~2-3,
                        workload-limited ~4, workhorse 6-7)
    bp_ip3            : team relief IP over the last 3 days (fatigue)
    woba_vs_hand      : lineup wOBA vs the OPPOSING starter's handedness
    woba_overall      : lineup overall wOBA (platoon gap baseline)
    ptype_rv100       : lineup run value /100 pitches vs the opposing
                        starter's specific pitch mix (see matchup_rv100)
    """
    name: str
    wins: int
    losses: int
    starter: str
    sp_rate: float
    bp_rate: float = LEAGUE_RATE
    sp_ip: float = DEFAULT_SP_IP
    bp_ip3: float = None
    woba_vs_hand: float = None
    woba_overall: float = None
    ptype_rv100: float = 0.0


def regressed_wpct(w: int, l: int) -> float:
    g = w + l
    return (w + 0.5 * REGRESSION_GAMES) / (g + REGRESSION_GAMES)


def log5(a: float, b: float) -> float:
    """P(team with true-talent win% a beats team with b)."""
    return (a * (1 - b)) / (a * (1 - b) + b * (1 - a))


def effective_bp_rate(t: TeamDay) -> float:
    rate = t.bp_rate
    if t.bp_ip3 is not None and t.bp_ip3 > FATIGUE_FREE_IP3:
        rate += (t.bp_ip3 - FATIGUE_FREE_IP3) * FATIGUE_RUNS_PER_IP
    return rate


def pitching_runs_vs_avg(t: TeamDay) -> float:
    sp_ip = min(max(t.sp_ip, 0.0), 9.0)
    return ((t.sp_rate - LEAGUE_RATE) * sp_ip
            + (effective_bp_rate(t) - LEAGUE_RATE) * (9 - sp_ip)) / 9


def offense_runs_vs_avg(t: TeamDay) -> float:
    """Runs the lineup adds vs the opposing starter beyond its overall
    quality (which W-L already prices in)."""
    runs = 0.0
    if t.woba_vs_hand is not None and t.woba_overall is not None:
        runs += (t.woba_vs_hand - t.woba_overall) / WOBA_TO_RUNS * PA_VS_STARTER
    runs += t.ptype_rv100 * PITCHES_VS_STARTER / 100
    return runs


def predict(home: TeamDay, away: TeamDay) -> float:
    """Home team win probability."""
    p = log5(regressed_wpct(home.wins, home.losses),
             regressed_wpct(away.wins, away.losses))
    logit = math.log(p / (1 - p)) + math.log(HFA_ODDS)
    delta = (pitching_runs_vs_avg(away) - pitching_runs_vs_avg(home)
             + offense_runs_vs_avg(home) - offense_runs_vs_avg(away))
    logit += (delta / RUNS_PER_WIN) * LOGODDS_PER_WIN
    return 1 / (1 + math.exp(-logit))


def report(home: TeamDay, away: TeamDay) -> str:
    p = predict(home, away)
    fav, prob = (home, p) if p >= 0.5 else (away, 1 - p)
    return f"{away.name} @ {home.name} -> {fav.name} {prob:.1%} (home {p:.1%})"


# ————————————————————————————————————————————————
# Platoon helpers (feed woba_vs_hand / woba_overall)
# Data: FanGraphs team-batting split exports (vs LHP / vs RHP CSVs), or
# per-player splits weighted by batting-order slot for confirmed lineups.
# ————————————————————————————————————————————————
SLOT_PA = [4.7, 4.6, 4.5, 4.4, 4.3, 4.2, 4.1, 4.0, 3.9]


def team_woba_vs_hand(splits_csv: str) -> dict:
    """FanGraphs team split export -> {team: wOBA}. Needs pandas."""
    import pandas as pd
    df = pd.read_csv(splits_csv)
    return dict(zip(df["Team"], df["wOBA"]))


def lineup_woba(player_wobas: list) -> float:
    """Nine hitters' wOBA vs the relevant hand, in batting order,
    weighted by how often each lineup slot bats."""
    w = SLOT_PA[: len(player_wobas)]
    return sum(x * y for x, y in zip(player_wobas, w)) / sum(w)


# ————————————————————————————————————————————————
# Pitch-type matchup (feeds ptype_rv100)
# Data: Savant team run-value-by-pitch-type CSV + the starter's mix.
# ————————————————————————————————————————————————
def team_rv100_by_pitch(savant_csv: str) -> dict:
    """{(team, pitch_type): run value per 100 pitches}. Needs pandas."""
    import pandas as pd
    df = pd.read_csv(savant_csv)
    return {(r["team_name"], r["pitch_type"]): r["run_value_per_100"]
            for _, r in df.iterrows()}


def matchup_rv100(team: str, pitcher_mix: dict, rv_table: dict) -> float:
    """pitcher_mix: {pitch_type: usage fraction, sums to ~1.0}.
    Positive = this lineup profiles well against this arsenal."""
    return sum(usage * rv_table.get((team, ptype), 0.0)
               for ptype, usage in pitcher_mix.items())


# ————————————————————————————————————————————————
# Bullpen fatigue (feeds bp_ip3) — MLB StatsAPI, run where internet works
# ————————————————————————————————————————————————
def relief_ip_last_days(days: int = 3) -> dict:
    """{full team name: relief IP over the last `days` days}."""
    import requests
    API = "https://statsapi.mlb.com/api/v1"
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    sched = requests.get(f"{API}/schedule",
                         params={"sportId": 1, "startDate": start,
                                 "endDate": end}, timeout=30).json()
    totals: dict = {}
    for d in sched.get("dates", []):
        for game in d.get("games", []):
            if game["status"]["abstractGameState"] != "Final":
                continue
            box = requests.get(f"{API}/game/{game['gamePk']}/boxscore",
                               timeout=30).json()
            for side in ("home", "away"):
                team = box["teams"][side]["team"]["name"]
                for pid in box["teams"][side]["pitchers"][1:]:  # skip SP
                    st = box["teams"][side]["players"][f"ID{pid}"]
                    ip = st["stats"]["pitching"].get("inningsPitched", "0")
                    whole, _, frac = ip.partition(".")
                    totals[team] = (totals.get(team, 0.0)
                                    + int(whole) + int(frac or 0) / 3)
    return totals


# ————————————————————————————————————————————————
# Pick log — calibration is the only test that matters
# ————————————————————————————————————————————————
PICKS_CSV = Path("picks.csv")
_FIELDS = ["date", "pick", "opponent", "prob", "won", "notes"]


def _rows() -> list:
    if not PICKS_CSV.exists():
        return []
    with PICKS_CSV.open() as f:
        return list(csv.DictReader(f))


def _save(rows: list) -> None:
    with PICKS_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writeheader()
        w.writerows(rows)


def log_pick(day: str, pick: str, opponent: str, prob: float,
             notes: str = "") -> None:
    rows = _rows()
    rows.append({"date": day, "pick": pick, "opponent": opponent,
                 "prob": f"{prob:.4f}", "won": "", "notes": notes})
    _save(rows)


def settle(day: str, pick: str, won: bool) -> None:
    rows = _rows()
    for r in rows:
        if r["date"] == day and r["pick"] == pick and r["won"] == "":
            r["won"] = "1" if won else "0"
    _save(rows)


def calibration_report() -> None:
    done = [r for r in _rows() if r["won"] != ""]
    if not done:
        print("no settled picks yet")
        return
    probs = [float(r["prob"]) for r in done]
    outs = [int(r["won"]) for r in done]
    n = len(done)
    brier = sum((p - o) ** 2 for p, o in zip(probs, outs)) / n
    print(f"settled picks : {n}")
    print(f"win rate      : {sum(outs)/n:.3f}   avg predicted: {sum(probs)/n:.3f}")
    print(f"brier score   : {brier:.4f}  (guessing 0.5 -> 0.2500)")
    print("\nby confidence bucket (pred -> actual, n):")
    for lo in (0.5, 0.6, 0.7, 0.8, 0.9):
        b = [(p, o) for p, o in zip(probs, outs) if lo <= p < lo + 0.1]
        if b:
            print(f"  {lo:.0%}-{lo+0.1:.0%}: {sum(p for p,_ in b)/len(b):.2f}"
                  f" -> {sum(o for _,o in b)/len(b):.2f}  (n={len(b)})")


# ————————————————————————————————————————————————
# Demo
# ————————————————————————————————————————————————
if __name__ == "__main__":
    # Jul 26 Sunday night game, v3-style inputs:
    home = TeamDay("Phillies", 62, 43, "Sanchez", 2.71, bp_rate=4.40, sp_ip=6.0)
    away = TeamDay("Yankees", 61, 44, "Warren", 4.00, bp_rate=3.01, sp_ip=4.5)
    print("v3 inputs :", report(home, away))

    # Same game with v4 platoon estimates engaged:
    home.woba_vs_hand, home.woba_overall = 0.325, 0.320   # PHI vs RHP
    away.woba_vs_hand, away.woba_overall = 0.298, 0.318   # NYY vs LHP
    print("v4 platoon:", report(home, away))
