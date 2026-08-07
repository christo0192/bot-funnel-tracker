# IK Bot Console — bot-calling funnel dashboard

Live: **https://bot-funnel-tracker.vercel.app**

A daily-refreshing dashboard over the bot-calling qualification funnel for IK India
(Phase 2). Reads the BigQuery view `ik-marketing-data.India_Leads.Bot_Calling_Phase2_leadwise`,
aggregates it, and renders a self-contained static page (baseline funnel metrics +
advanced business-impact views). Denominator rule: every headline % is a share of Leads.

## How the refresh works
A scheduled GitHub Action (`.github/workflows/refresh.yml`, daily 02:00 UTC / 07:30 IST):
1. `pipeline/build_data.py` — one scan of the view → `data.json` (aggregates: daily
   29-metric matrix, per-dimension breakdowns, histograms, sample rows).
2. `pipeline/inject.py` — injects `data.json` into `dashboard/template.html` → `site/index.html`.
3. `vercel deploy --prod` — publishes the static `site/` to the `bot-funnel-tracker` project.

No servers, no build step on Vercel — `site/index.html` is the whole app.

## Secrets (GitHub → Settings → Secrets → Actions)
- `GCP_SA_KEY` — service-account JSON with read access to the view.
- `VERCEL_TOKEN` — Vercel deploy token.
- `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` — from the Vercel project.

## Local / manual refresh (optional)
`pipeline/refresh.sh` (WSL) or `pipeline/refresh.ps1` (Windows) do the same build+deploy
locally. See `pipeline/VERCEL-SETUP.md`.

## Source of truth for logic
The qualification model (gates, outcomes, booking sub-statuses) is defined in the
project SOP (kept out of this repo).
