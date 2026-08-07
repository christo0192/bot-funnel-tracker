# Vercel setup

The dashboard deploys as a **static site**: `site/index.html` (built by `inject.py`)
is the whole app — self-contained HTML/JS/SVG, data baked in. No framework, no
serverless functions, no build step. The daily job rebuilds it from BigQuery and
runs `vercel deploy --prod`, fully headless.

## Already done (via herdr, 2026-08-07)
- Logged in as **christo.b@interviewkickstart.com** (Vercel user `christob-4872`,
  team "christob-4872's projects").
- Project **bot-funnel-tracker** created and linked (`site/.vercel/project.json`).
- First production deploy is **live: https://bot-funnel-tracker.vercel.app**

So the only thing left to make it refresh **daily and hands-off** is giving the
Windows scheduled task a way to deploy non-interactively.

## Finish the automation (Windows)

### 1. Install the Vercel CLI for the Windows cron (needs Node.js)
```powershell
npm i -g vercel
```

### 2. Create a deploy token (under the christo.b account)
Vercel dashboard -> Settings -> Tokens -> Create Token (scope: christob-4872's
projects). Copy it, then store as a user env var (keep secret):
```powershell
setx VERCEL_TOKEN "PASTE_TOKEN_HERE"
# open a NEW PowerShell window after setx so it is in scope
```

### 3. Test one full refresh + deploy
```powershell
cd "C:\Users\Admin\Claude projects\Bot calling tracker"
powershell -NoProfile -ExecutionPolicy Bypass -File "pipeline\refresh.ps1"
# build_data (BigQuery) -> inject -> vercel deploy --prod --cwd site
# updates https://bot-funnel-tracker.vercel.app
```

### 4. Schedule it daily (elevated PowerShell)
```powershell
cd "C:\Users\Admin\Claude projects\Bot calling tracker"
powershell -NoProfile -ExecutionPolicy Bypass -File "pipeline\register-task.ps1"
# add -At "06:00" to change the time; the task reads VERCEL_TOKEN from the environment
```

## Notes
- The BigQuery scan (~50s) runs locally with the service-account key — no serverless time limits.
- Only `site/` is deployed; `.vercelignore` keeps `.env.local`/`.vercel` out. The PDF,
  data.json, and pipeline scripts never touch the web.
- Deploy on demand any time: `powershell -File "pipeline\refresh.ps1"`.
- The project link (`site/.vercel/project.json`) is shared between WSL and Windows
  (same folder), so both deploy to the same project.
- Alternative to a token: run `vercel login` once in Windows PowerShell (interactive),
  then `refresh.ps1` deploys using stored creds even without VERCEL_TOKEN.
