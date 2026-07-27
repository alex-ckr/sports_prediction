# v4 — MLB game model, fed automatically

The v4 model with every input pulled from free public APIs, a slate of upcoming
games, and each pick priced against Kalshi.

## How it's wired

```
GitHub Actions (cron)          GitHub Pages (static)
  build_slate.py        ──▶     data/slate.json    ──▶   index.html
  MLB StatsAPI + Kalshi          committed to repo        model.js
```

The fetching happens in Python on Actions, not in the browser. That's deliberate:
Kalshi's market data is public but sends no CORS headers you can rely on, and a
browser can't be argued out of a preflight failure. Doing it server-side also means
no keys in client code, no rate limits tied to your IP, and a slate that's already
there when the page loads.

| File | Role |
|---|---|
| `build_slate.py` | The ETL. Every network call lives here |
| `.github/workflows/update-slate.yml` | Runs it four times a day, commits the result |
| `watch_lineups.py` | Fires on confirmed lineups, rebuilds offense, emails the pick |
| `.github/workflows/lineup-alerts.yml` | Polls every 10 min during the game window |
| `data/slate.json` | The snapshot the page reads (created on first run) |
| `data/slate.sample.json` | Synthetic file for checking layout. Not real data |
| `index.html`, `model.js` | The interface and the ported model |
| `mlb_model_v4.py` | The original, unchanged |

## What gets fetched, and how each input is derived

| Model input | Source | Derivation |
|---|---|---|
| `wins` / `losses` | StatsAPI standings | direct |
| `starter` | StatsAPI schedule, `hydrate=probablePitcher` | direct |
| `sp_rate` | StatsAPI season pitching | FIP computed from HR, BB, HBP, K, IP |
| `sp_ip` | same | season IP ÷ games started |
| `bp_rate` | same | every arm with GS/G < 0.5, aggregated, then FIP |
| `bp_ip3` | StatsAPI boxscores, last 3 days | relief IP, starter excluded |
| `woba_vs_hand` | team splits pre-game, **posted lineup** once confirmed | wOBA rebuilt from raw counting stats, slot-weighted |
| `woba_overall` | StatsAPI team season hitting | same formula |
| `ptype_rv100` | — | **not automated**, defaults to 0 |

Two things worth knowing about the derivations.

**FIP isn't in StatsAPI, so it's computed.** `(13·HR + 3·(BB+HBP) − 2·K) ÷ IP + constant`,
where the constant is derived each run from league totals so that league FIP equals
league ERA — rather than hardcoding 3.10 and drifting from it.

**wOBA isn't in StatsAPI either.** It's rebuilt from the component counting stats with
standard linear weights, which *are* hardcoded in `W` at the top of `build_slate.py`.
Refresh them once a season from FanGraphs' guts table.

**Pitch-type run values are still manual.** They live on Baseball Savant, which has no
stable free JSON endpoint, so `ptype_rv100` stays 0 unless you type it in on the Inputs
tab. That term contributes nothing until you do.

## Kalshi

Public unauthenticated read of `GET /markets?series_ticker=KXMLBGAME&status=open`.
No account, no key.

Markets are matched to games by content, not by ticker format — both team abbreviations
must appear in the ticker, then the YES side is resolved from the ticker tail or subtitle.
Kalshi's ticker format isn't a contract and may change; a game that can't be matched shows
a dash rather than a wrong price, and an ambiguous YES side is flagged loudly in the
market panel. `ABBR` in `build_slate.py` is the mapping to adjust if it drifts.

EV is net of Kalshi's fee, `0.07 × price × (1 − price)` rounded up to the cent, and is
computed for both buying YES at the ask and buying NO at one minus the bid.



## The edge board

The landing tab ranks every priced game by **net edge** and lets you click through to the
full breakdown. Four columns matter:

| Column | What it is |
|---|---|
| Raw | Model minus Kalshi's mid. What the disagreement looks like before costs |
| Hurdle | The ask plus Kalshi's fee, as points above the mid. What you pay to get filled |
| Net | Raw minus hurdle. The only one that can be positive and mean something |
| Inputs | How many of the six input groups the feed actually supplied |

Raw edge is the number that feels like profit and isn't. Two games can show an identical
four-point raw edge and land on opposite sides of zero:

```
model 60.0%  mid 56.0%  bid 55 / ask 57   hurdle 3.0pts  ->  net +1.0
model 60.0%  mid 56.0%  bid 52 / ask 60   hurdle 6.0pts  ->  net −2.0
```

On the tight market the break-even is 59%; on the wide one it's 62%. And a game where the
model looks like it *agrees* with the market at +0.1 raw is −2.9 net — there's no trade
there at any size. Sorting by raw disagreement hides all of this.

Sorting by raw disagreement does something worse too. The games where a model most disagrees
with a liquid market are disproportionately the games where the model has bad inputs: a
scratched starter, a bullpen rate built on twenty innings, a platoon split with no sample
behind it. Rank by disagreement and you are sorting your slate toward your own broken data.
That's why the coverage dots sit next to the edge and why **Fully fed only** exists — turn it
on before reading anything.

**Refresh prices** re-reads each market from Kalshi in the browser. It may be blocked by
CORS, in which case prices update on the workflow's schedule instead; the button reports
which happened rather than failing quietly.

## Confirmed lineups

`build_slate.py` fills `woba_vs_hand` from the team's **season** split, which assumes a
full-strength lineup. When two regulars sit, that number doesn't move at all. Lineups post
roughly two to four hours before first pitch, so the pre-game slate is always working off
an assumption.

`watch_lineups.py` closes that gap. Every ten minutes during the game window it checks each
upcoming game's boxscore for a posted batting order. Once both sides show nine hitters it:

1. Reads the **confirmed** starter from `pitchers[0]` rather than the days-old probable, and
   flags it if the arm changed
2. Pulls each of the eighteen hitters' wOBA vs the opposing starter's hand
3. Regresses every split toward that hitter's own overall line with 300 PA of ballast —
   platoon splits are noisy enough that a hot 40-PA sample is nearly all noise
4. Weights the nine by lineup slot with the same `SLOT_PA` table the model uses
5. Re-prices the Kalshi market, runs the model, emails the result, and writes the new inputs
   back into `data/slate.json`

Because `woba_overall` stays the team's season figure, the `(vs-hand − overall)` delta now
carries **both** the platoon effect and today's lineup strength. A lineup missing its two
best bats produces a negative offense term on its own, with no extra machinery.

The email says what changed:

```
PHI lineup wOBA vs RHP 0.3312 (team season split was 0.3250, +6 points)
NYY starter changed: Will Warren -> Clarke Schmidt
```

### Email setup

Add these as repository secrets under Settings → Secrets and variables → Actions:

| Secret | Example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | your address |
| `SMTP_PASS` | a Gmail **app password**, not your account password |
| `ALERT_FROM` | your address |
| `ALERT_RECIPIENTS` | `you@x.com,friend@y.com` |

Never put these in the repo. The workflow reads them from the secret store and the script
only ever sees them as environment variables.

Run *Lineup alerts* manually with `dry_run` checked first — it prints the email instead of
sending it, so you can see the composition before anyone's inbox is involved.

### Timing caveat

GitHub's cron scheduler is best-effort, and under load it routinely fires five to thirty
minutes late. Practically that means "within about fifteen minutes of lineups posting,"
which is fine for a one-hour-out alert and not fine if you ever want to react to the lineup
faster than the market does. If that becomes the goal, the same script on a small always-on
box with a one-minute loop is the fix — nothing about it depends on Actions.

State lives in `data/alerts_sent.json`, so each game mails once. Delete an entry to re-send.

## Deploy

```bash
git init && git add . && git commit -m "automated v4"
git remote add origin https://github.com/<you>/<repo>.git && git push -u origin main
```

1. **Settings → Pages** → Deploy from a branch → `main` / root
2. **Settings → Actions → General → Workflow permissions** → Read and write
3. **Actions → Refresh slate → Run workflow** to populate `data/slate.json` immediately

Until step 3 runs, the Slate tab shows an empty state. The "Fetch schedule live" button
gets you records and probable starters straight from the browser in the meantime — no
pitching, no splits, no market.

Run it locally the same way: `pip install requests && python build_slate.py --days 2`.

## Reading the slate

The `Inputs` column shows filled dots for how many of the six input groups the feed
actually supplied. Six dots means a fully-fed prediction. Three dots means the model is
running mostly on defaults and its number is closer to a record-and-home-field guess than
a real estimate. Check the dots before reading the edge.

## On the edge column

The disagreement between the model and Kalshi is a debugging output before it is anything
else. KXMLBGAME is a liquid market with real money on both sides; the prior should be that
it is better calibrated than eight hand-tuned constants. A 10-point gap almost always means
a stale probable pitcher, a bullpen rate computed over 20 innings, or a platoon split with
no sample behind it — not a mispriced game.

The pick log is the only thing that can change that prior. Log picks, settle them, and
watch whether the buckets track. Until the Brier score and the bucket table say the model
is calibrated, the edge column is a list of places to look for bugs.
