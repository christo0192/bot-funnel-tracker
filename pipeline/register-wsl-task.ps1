<#
  register-wsl-task.ps1 - register a daily Windows task that runs the refresh
  INSIDE WSL (where the Vercel CLI is already logged in as christo.b and the
  BigQuery key + python live). No Vercel token needed.

  Run once. If it errors about access/privileges, run in an elevated PowerShell.
  Default time 07:30; change with -At "06:00".
  Remove with: Unregister-ScheduledTask -TaskName "IK Bot Console Refresh" -Confirm:$false
#>
param([string]$At = "07:30")

$wsl   = Join-Path $env:SystemRoot "System32\wsl.exe"
$inner = "cd '/mnt/c/Users/Admin/Claude projects/Bot calling tracker' && bash pipeline/refresh.sh"
$arg   = "-e bash -lc `"$inner`""

$action   = New-ScheduledTaskAction -Execute $wsl -Argument $arg
$trigger  = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "IK Bot Console Refresh" -Action $action -Trigger $trigger `
  -Settings $settings -Description "Daily BigQuery -> Vercel refresh via WSL (bot-funnel-tracker)" -Force
Write-Host "Registered 'IK Bot Console Refresh' daily at $At (runs refresh.sh in WSL)."
Write-Host "Test now: Start-ScheduledTask -TaskName 'IK Bot Console Refresh'"
