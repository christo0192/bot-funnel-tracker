<#
  refresh.ps1 - daily rebuild + deploy of the IK Bot Console dashboard.
  Runs on the local Windows machine (has the BigQuery service-account key).

  Steps:
    1. build_data.py  - scan the BigQuery view once -> data.json
    2. inject.py      - data.json -> site/index.html (+ dashboard/console.html)
    3. vercel deploy  - deploy the static site/ folder to production (headless)

  Note: Python prints progress to stdout; we gate success on the process exit
  code ($LASTEXITCODE), NOT on whether anything was written to stderr - otherwise
  a harmless progress line would look like a failure.

  One-time setup before the first deploy: see pipeline/VERCEL-SETUP.md.
  Register as a daily task with pipeline/register-task.ps1.
#>
param(
  [string]$Key = "C:\Users\Admin\.gcp-keys\claude-code-dev-bq.json",
  [string]$VercelToken = $env:VERCEL_TOKEN,  # token for headless deploy (or set VERCEL_TOKEN env var)
  [switch]$NoDeploy                          # build + inject only; skip the Vercel deploy
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$log = Join-Path $PSScriptRoot "last-refresh.log"
$env:GOOGLE_APPLICATION_CREDENTIALS = $Key
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Log([string]$m) { $line = "[{0}] {1}" -f (Get-Date -Format o), $m; Write-Host $line; Add-Content -Path $log -Value $line }

# Run a command, stream its merged output to the log, and throw on non-zero exit.
function Run([string]$label, [scriptblock]$cmd) {
  Log "START $label"
  & $cmd 2>&1 | ForEach-Object { $s = "$_"; Write-Host $s; Add-Content -Path $log -Value $s }
  if ($LASTEXITCODE -ne 0) { throw "$label failed (exit $LASTEXITCODE) - see $log" }
  Log "OK    $label"
}

"" | Set-Content -Path $log   # fresh log each run
Log "refresh start"

Run "build_data" { python "pipeline\build_data.py" --out "data.json" }
Run "inject"     { python "pipeline\inject.py" }

if (-not $NoDeploy) {
  if (-not $VercelToken) { throw "No Vercel token. Pass -VercelToken or set $env:VERCEL_TOKEN (see pipeline/VERCEL-SETUP.md)." }
  # Deploy the static site/ folder to production. --cwd makes vercel read
  # site/.vercel/project.json (project "bot-funnel-tracker"), so it is non-interactive
  # and always targets the right project regardless of the shell's working dir.
  Run "deploy" { vercel deploy --prod --yes --cwd "site" --token $VercelToken }
}

Log "refresh done"
