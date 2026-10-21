param([ValidateSet('Install','Remove','Status')][string]$Action='Status')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workerPath = Join-Path $projectRoot 'Start-Scheduler.ps1'
$hasher = [System.Security.Cryptography.SHA256]::Create()
$digest = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Replace('-','').Substring(0,10)
$taskName = "TradingAgents-LocalScheduler-$digest"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and -not ($existing.Actions.Arguments -contains "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$workerPath`"")) {
    throw 'The task name belongs to a different action. It was left untouched.'
}
if ($Action -eq 'Remove') {
    if ($existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    @{state='not_required';task_name=$taskName;message='No active Windows scheduler heartbeat.'} | ConvertTo-Json -Compress
    exit 0
}
if ($Action -eq 'Install') {
    $powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$workerPath`""
    $taskAction = New-ScheduledTaskAction -Execute $powerShellPath -Argument $arguments -WorkingDirectory $projectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 55) -MultipleInstances IgnoreNew -Hidden
    $userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal -Description 'Keeps the local TradingAgents scheduler available while the user is logged in. Does not itself place trades.' -Force | Out-Null
    $existing = Get-ScheduledTask -TaskName $taskName
}
@{state=$(if($existing){'installed'}else{'not_required'});task_name=$taskName;message=$(if($existing){'Windows checks every minute. PC must be awake and this user logged in; the browser can be closed.'}else{'No background task installed.'})} | ConvertTo-Json -Compress
