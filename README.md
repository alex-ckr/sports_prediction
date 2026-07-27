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
| `woba_vs_hand` | StatsAPI team splits, `sitCodes=vl,vr` | wOBA rebuilt from raw counting stats |
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
