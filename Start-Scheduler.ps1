$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path -LiteralPath (Join-Path $projectRoot '.scheduler-maintenance')) { exit 0 }
try {
    $status = Invoke-RestMethod 'http://127.0.0.1:8765/api/status' -TimeoutSec 3
    if ($status) { exit 0 }
} catch { }
& (Join-Path $projectRoot 'Start-LocalUI.ps1') -NoBrowser
