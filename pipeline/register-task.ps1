<#
  register-task.ps1 - one-time: register the daily refresh with Windows Task Scheduler.
  Run in an elevated PowerShell (Run as Administrator). Default: every day 07:30 IST.
  The task reads VERCEL_TOKEN from the environment unless you pass -VercelToken here.
  Change -At to your preferred time. Remove with:
     Unregister-ScheduledTask -TaskName "IK Bot Console Refresh" -Confirm:$false
#>
param(
  [string]$At = "07:30",
  [string]$VercelToken = ""   # optional: bake the token into the task action
)
$refresh = Join-Path $PSScriptRoot "refresh.ps1"
$tokenArg = if ($VercelToken) { " -VercelToken `"$VercelToken`"" } else { "" }
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$refresh`"$tokenArg"
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "IK Bot Console Refresh" -Action $action `
  -Trigger $trigger -Settings $settings -Description "Daily rebuild+deploy of the IK Bot Console dashboard from BigQuery to Vercel" -Force
Write-Host "Registered 'IK Bot Console Refresh' daily at $At. Test now with: Start-ScheduledTask -TaskName 'IK Bot Console Refresh'"
