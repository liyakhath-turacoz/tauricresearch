param([switch]$Stop, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$serverPath = Join-Path $projectRoot 'local_ui.py'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pidPath = Join-Path $env:TEMP 'TradingAgentsLocalUI.pid'
$url = 'http://127.0.0.1:8765'
$hasher = [Security.Cryptography.SHA256]::Create()
$projectId = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Replace('-','').Substring(0,10)
$hasher.Dispose()
$launcherMutex = [Threading.Mutex]::new($false, "Local\TradingAgentsUiLauncher-$projectId")
$ownsMutex = $false

function Get-UiStatus([int]$Timeout = 2) {
    try { return Invoke-RestMethod "$url/api/status" -TimeoutSec $Timeout } catch { return $null }
}
function Test-OwnedProcess($Info) {
    if (-not $Info -or -not $Info.CommandLine -or $Info.Name -notmatch '^python[^\s]*\.exe$') { return $false }
    $prefix = '^(?:"[^"]+"|\S+)\s+(?:(?:-u|-B|-E|-I|-s|-S|-O|-OO)\s+)*'
    $absoluteScript = '(?:"' + [regex]::Escape($serverPath) + '"|' + [regex]::Escape($serverPath) + ')(?=\s|$)'
    if ($Info.CommandLine -match ($prefix + $absoluteScript)) { return $true }
    $relativeScript = '"?(?:\.[\\/])?local_ui\.py"?(?=\s|$)'
    return $matchingStatus -and $Info.ProcessId -eq $status.server_pid -and $Info.CommandLine -match ($prefix + $relativeScript)
}
try {
    try { $ownsMutex = $launcherMutex.WaitOne(20000) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Another launch/stop operation is still in progress. Wait a moment and try again.' }
    $serverPid = 0
    if (Test-Path -LiteralPath $pidPath) {
        [void][int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$serverPid)
    }
    $info = if ($serverPid -gt 0) { Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue } else { $null }
    $status = Get-UiStatus
    $matchingStatus = $status -and $status.app_id -eq 'TradingAgentsLocalUI' -and $status.project_id -eq $projectId
    if (-not (Test-OwnedProcess $info) -and $matchingStatus -and $status.server_pid) {
        $serverPid = [int]$status.server_pid
        $info = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
    }
    if ($Stop) {
        if ($info -and -not (Test-OwnedProcess $info)) {
            throw 'The PID belongs to another process/project; it was left untouched.'
        }
        if (-not $info) {
            if (Test-Path -LiteralPath $pidPath) { Remove-Item -LiteralPath $pidPath -Force }
            Write-Host 'No running TradingAgents UI process was found.'
            return
        }
        $enabledSchedules = $false
        try {
            $page = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing
            $tokenMatch = [regex]::Match($page.Content, 'name="local-ui-token" content="([^"]+)"')
            if ($tokenMatch.Success) {
                $headers = @{'X-Local-UI-Token'=$tokenMatch.Groups[1].Value; 'Origin'=$url}
                Invoke-RestMethod -Uri "$url/api/login" -Method Post -Headers $headers -Body '{"email":"admin@turacoz.com","password":"admin"}' -ContentType 'application/json' -SessionVariable uiSession -TimeoutSec 4 | Out-Null
                $savedSchedules = Invoke-RestMethod "$url/api/schedules" -WebSession $uiSession -TimeoutSec 2
                $enabledSchedules = @($savedSchedules.schedules | Where-Object { $_.enabled }).Count -gt 0
                Invoke-RestMethod -Uri "$url/api/shutdown" -Method Post -Headers $headers -WebSession $uiSession -Body '{}' -ContentType 'application/json' -TimeoutSec 4 | Out-Null
                Wait-Process -Id $serverPid -Timeout 12 -ErrorAction SilentlyContinue
            }
        } catch { }
        $remaining = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
        if (Test-OwnedProcess $remaining) {
            # If graceful shutdown could not respond, stop only this server's CLI child.
            foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $serverPid" -ErrorAction SilentlyContinue)) {
                if ($child.ExecutablePath -eq $pythonPath -and $child.CommandLine -match '-m\s+cli\.main') {
                    Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
                }
            }
            Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Write-Host 'TradingAgents local UI stopped.'
        if ($enabledSchedules) { Write-Host 'Enabled schedules may restart it within a minute. Use Schedules > Pause all schedules to keep it stopped.' }
        return
    }
    if (Test-Path -LiteralPath (Join-Path $projectRoot '.scheduler-maintenance')) { throw 'Scheduled maintenance is in progress. Try again after it finishes.' }
    if ($matchingStatus -or ((Test-OwnedProcess $info) -and $status -and -not $status.app_id)) {
        if ($status.accepting_runs -eq $false) { throw 'The server is in maintenance. Finish that operation or stop/restart the UI.' }
        if (-not $NoBrowser) { Start-Process $url }
        Write-Host "TradingAgents local UI is already running: $url"
        return
    }
    if ($status) { throw 'Port 8765 is serving another project/application. It was left untouched.' }
    if (Test-OwnedProcess $info) { throw 'The UI process is present but not responding. Run Start-LocalUI.ps1 -Stop, then launch again.' }
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Virtual environment missing. Install the project into .venv first.' }
    if (-not (Test-Path -LiteralPath $serverPath)) { throw "Missing local UI bridge: $serverPath" }
    if (Test-Path -LiteralPath $pidPath) { Remove-Item -LiteralPath $pidPath -Force }
    $stdoutPath = Join-Path $env:TEMP 'TradingAgentsLocalUI.stdout.log'
    $stderrPath = Join-Path $env:TEMP 'TradingAgentsLocalUI.stderr.log'
    $quotedScript = '"' + $serverPath + '"'
    $proc = Start-Process -FilePath $pythonPath -ArgumentList @($quotedScript) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 300
        $proc.Refresh()
        if ($proc.HasExited) { break }
        $readyStatus = Get-UiStatus 1
        if ($readyStatus -and $readyStatus.app_id -eq 'TradingAgentsLocalUI' -and $readyStatus.project_id -eq $projectId) { $ready = $true; break }
    }
    if (-not $ready) {
        $unready = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue
        if (Test-OwnedProcess $unready) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        throw "Local UI did not start. Check $stderrPath and that port 8765 is free."
    }
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host "TradingAgents local UI is running: $url"
    Write-Host 'Stop it from the page, or run .\Start-LocalUI.ps1 -Stop.'
} finally {
    if ($ownsMutex) { $launcherMutex.ReleaseMutex() }
    $launcherMutex.Dispose()
}
