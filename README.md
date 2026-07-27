# v4 — MLB game model, in the browser

A client-side port of `mlb_model_v4.py`. No server, no build step, no dependencies.
Everything (prediction, helpers, pick log) runs in the tab.

## Files

| File | What's in it |
|---|---|
| `index.html` | The whole interface |
| `model.js` | The model, ported function-for-function from the Python |
| `mlb_model_v4.py` | The original, kept for reference and offline runs |

## Run it locally

Open `index.html` in a browser. That's it — no local server needed.

## Publish on GitHub Pages

```bash
git init
git add index.html model.js README.md
git commit -m "v4 model in the browser"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then in the repo: **Settings → Pages → Source: Deploy from a branch → `main` / `(root)` → Save**.
The site appears at `https://<you>.github.io/<repo>/` in about a minute.

Make the repo private if you'd rather nobody else read your numbers — Pages still works
on private repos for GitHub Pro accounts.

## Parity with the Python

Identical to the last decimal on the demo matchup:

| Case | Python | Browser |
|---|---|---|
| v3 inputs | 0.5719280203217414 | 0.5719280203217414 |
| + platoon | 0.6261671891541153 | 0.6261671891541153 |
| + fatigue and pitch mix | 0.6654792936037397 | 0.6654792936037397 |

## Notes

- **Bullpen fatigue** calls MLB's public stats API directly from the page. If the
  browser blocks it as a cross-origin request, run `relief_ip_last_days()` from the
  Python file and type the innings in by hand.
- **The pick log** lives in this browser's local storage, and exports to the same
  six-column `picks.csv` the Python logger writes — so the file moves between the two
  in either direction.
- **Constants** are editable in the Inputs tab and persist per browser. Reset returns
  every knob to the values in the Python file.
