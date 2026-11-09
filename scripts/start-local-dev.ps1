# Start the local dev stack as detached processes (api / admin client / admin spa / n8n backend).
#
# Why WMI instead of Start-Process: children of this shell stay in its process
# tree, so they die when the shell goes away (tool timeout, dropped session,
# closed window). Processes created through Win32_Process hang off WmiPrvSE and
# keep running regardless.
#
# Why node instead of npm: npm.cmd locates its own node_modules through %~dp0,
# which breaks when launched this way (Cannot find module ...\node_modules\npm\bin\npm-cli.js).
#
#   powershell -ExecutionPolicy Bypass -File scripts\start-local-dev.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start-local-dev.ps1 -Stop
#
# Logs: %TEMP%\gainlab-dev\<name>.log
# ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.

param(
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$logDir = Join-Path $env:TEMP 'gainlab-dev'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$apiRoot = 'e:\web\python\gainlabaiapi'
$clientRoot = 'E:\web\react\gainlab-aitrader'
$adminRoot = 'E:\web\vue\gainlabn8n-admin'
$n8nRoot = 'E:\web\node\gainlabn8n-serve'

$nodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if (-not $nodeExe) {
    foreach ($candidate in @('C:\nvm4w\nodejs\node.exe', 'C:\Program Files\nodejs\node.exe')) {
        if (Test-Path $candidate) { $nodeExe = $candidate; break }
    }
}
if (-not $nodeExe) {
    $found = (where.exe node 2>$null | Select-Object -First 1)
    if ($found) { $nodeExe = $found.Trim() }
}
if (-not $nodeExe) { throw 'node.exe not found' }

$services = @(
    @{ name = 'api';    port = 8800; workdir = $apiRoot;    exe = (Join-Path $apiRoot '.venv\Scripts\python.exe'); args = @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8800') }
    @{ name = 'client'; port = 5180; workdir = $clientRoot; exe = $nodeExe; args = @((Join-Path $clientRoot 'node_modules\vite\bin\vite.js'), '--host', '127.0.0.1', '--port', '5180') }
    @{ name = 'admin';  port = 5173; workdir = $adminRoot;  exe = $nodeExe; args = @((Join-Path $adminRoot 'node_modules\vite\bin\vite.js'), '--host', '127.0.0.1', '--port', '5173') }
    @{ name = 'n8n';    port = 3000; workdir = $n8nRoot;    exe = $nodeExe; args = @((Join-Path $n8nRoot 'node_modules\@nestjs\cli\bin\nest.js'), 'start', '--watch') }
)

function Test-Port([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

if ($Stop) {
    foreach ($svc in $services) {
        $conn = Get-NetTCPConnection -LocalPort $svc.port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            Write-Host ("stopping {0} (port {1}, pid {2})" -f $svc.name, $svc.port, $conn.OwningProcess)
            taskkill /PID $conn.OwningProcess /T /F 2>$null | Out-Null
        } else {
            Write-Host ("{0} is not running" -f $svc.name)
        }
    }
    return
}

foreach ($svc in $services) {
    if (Test-Port $svc.port) {
        Write-Host ("{0} already listens on {1}, skipped" -f $svc.name, $svc.port)
        continue
    }
    $out = Join-Path $logDir ("{0}.log" -f $svc.name)
    $err = Join-Path $logDir ("{0}.err.log" -f $svc.name)
    $argLine = (($svc.args | ForEach-Object { '"{0}"' -f $_ }) -join ' ')
    $cmd = 'cmd.exe /c cd /d "{0}" && "{1}" {2} 1>"{3}" 2>"{4}"' -f $svc.workdir, $svc.exe, $argLine, $out, $err
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
    if ($result.ReturnValue -ne 0) {
        Write-Host ("failed to start {0}: ReturnValue={1}" -f $svc.name, $result.ReturnValue)
        continue
    }
    Write-Host ("started {0} (port {1}) pid={2} log={3}" -f $svc.name, $svc.port, $result.ProcessId, $out)
}

Write-Host ''
Write-Host 'waiting for ports...'
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    $pending = @($services | Where-Object { -not (Test-Port $_.port) } | ForEach-Object { $_.name })
} while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline)

foreach ($svc in $services) {
    $state = if (Test-Port $svc.port) { 'OK' } else { 'FAILED' }
    Write-Host ("  {0,-7} :{1,-5} {2}" -f $svc.name, $svc.port, $state)
}

if ($pending.Count -gt 0) {
    Write-Host ''
    Write-Host ("not ready: {0} - see logs in {1}" -f ($pending -join ', '), $logDir)
}
