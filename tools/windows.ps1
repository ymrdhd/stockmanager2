# Stock Manager 09B: explicit single-machine local operations (PowerShell 5.1+).
# Paths are literal; no public listener or automatic formal initialization.
param(
  [Parameter(Mandatory=$true)][ValidateSet('Install','Build','Start','Stop','Check','Backup','DailyBackup','ScheduleBackup','Demo','DemoStart','VerifyMarket','Maintenance')][string]$Action,
  [string]$Destination,
  [ValidateRange(1,65535)][int]$Port=9091,
  [string[]]$MaintenanceArgs=@()
)
$ErrorActionPreference='Stop'
$Root=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $Root
$Py=Join-Path $Root '.venv\Scripts\python.exe'
$Start=Join-Path $Root 'start.py'
$Data=Join-Path $Root 'data'
$PidFile=Join-Path $Data 'windows-server-process.json'
function Run-Native([string]$Exe,[string[]]$Arguments) {
  & $Exe @Arguments
  if ($LASTEXITCODE -ne 0) { throw "$Exe returned exit code $LASTEXITCODE" }
}
function Require-Python { if (!(Test-Path -LiteralPath $Py -PathType Leaf)) {throw 'Run Install first'} }
function Assert-Node {
  $version=(& node --version); if ($LASTEXITCODE -ne 0 -or $version -notmatch '^v22\.(\d+)\.\d+$' -or [int]$Matches[1] -lt 13) {throw 'Use Node.js >=22.13.0 <23 (reference 22.16.0)'}
  $npmVersion=(& npm.cmd --version); if ($LASTEXITCODE -ne 0 -or $npmVersion -ne '10.9.2') {throw 'Use npm 10.9.2, the reference locked-install environment'}
}
function Build-Web {
  Assert-Node
  Push-Location -LiteralPath (Join-Path $Root 'frontend')
  try {
    Run-Native 'npm.cmd' @('ci')
    Run-Native 'npm.cmd' @('run','typecheck')
    Run-Native 'npm.cmd' @('run','build')
    if (!(Test-Path -LiteralPath 'dist\index.html' -PathType Leaf)) {throw 'Production dist missing'}
  } finally {Pop-Location}
}
function Get-ManagedProcess {
  if (!(Test-Path -LiteralPath $PidFile)) {return $null}
  $record=Get-Content -LiteralPath $PidFile -Raw | ConvertFrom-Json
  $proc=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
  if (!$proc) {return $null}
  $cim=Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)"
  if (!$cim -or $proc.StartTime.ToUniversalTime().Ticks.ToString() -ne $record.start_ticks -or !$cim.CommandLine -or !$cim.CommandLine.Contains($Start) -or !$cim.CommandLine.Contains(' server ')) {throw 'PID identity mismatch; do not kill or remove automatically'}
  return $proc
}
switch ($Action) {
 'Install' { Assert-Node; Run-Native 'uv' @('sync','--locked'); Build-Web }
 'Build' { Build-Web }
 'Check' { Require-Python; Run-Native $Py @($Start,'check-database') }
 'Start' {
   Require-Python
   if (!(Test-Path -LiteralPath (Join-Path $Root 'frontend\dist\index.html'))) {throw 'Build the production frontend first'}
   if (Get-ManagedProcess) {throw 'Managed server already running'}
   Run-Native $Py @($Start,'check-database')
   New-Item -ItemType Directory -Path $Data -Force | Out-Null
   # Start-Process joins arguments: quote paths explicitly, including spaces/Chinese.
   $arguments='"'+$Start+'" server --port '+$Port
   $proc=Start-Process -FilePath $Py -ArgumentList $arguments -WorkingDirectory $Root -PassThru
   $record=@{pid=$proc.Id;start_ticks=$proc.StartTime.ToUniversalTime().Ticks.ToString()}
   $record | ConvertTo-Json | Set-Content -LiteralPath $PidFile -Encoding UTF8
   Write-Host "Process launched; verify login and /health on http://127.0.0.1:$Port (not yet certified healthy)."
 }
 'Stop' {
   $proc=Get-ManagedProcess
   if ($proc) { Write-Warning 'Forced termination of the verified managed process; prefer Ctrl+C in its console for graceful shutdown.'; Stop-Process -Id $proc.Id; $proc.WaitForExit() }
   if (Test-Path -LiteralPath $PidFile) {Remove-Item -LiteralPath $PidFile}
   Write-Host 'Managed process stopped/absent. Independently stop any other writers before maintenance.'
 }
 'Backup' { Require-Python; if (!$Destination) {throw 'Provide a new absolute backup directory'}; Run-Native $Py @($Start,'backup','--destination',$Destination) }
 'DailyBackup' { Require-Python; $args2=@($Start,'backup-daily'); if ($Destination) {$args2+=@('--destination',$Destination)}; Run-Native $Py $args2 }
 'ScheduleBackup' {
   Require-Python
   # Current Windows operator, daily local 02:00, start when available, no stored password.
   $name='StockManager-DailyBackup-'+([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Root))).Replace('/','_').Replace('+','-').TrimEnd('=')
   $taskAction=New-ScheduledTaskAction -Execute $Py -Argument ('"'+$Start+'" backup-daily') -WorkingDirectory $Root
   $trigger=New-ScheduledTaskTrigger -Daily -At 2am
   $settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
   Register-ScheduledTask -TaskName $name -Action $taskAction -Trigger $trigger -Settings $settings -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -Description 'Stock Manager verified daily SQLite backup' -ErrorAction Stop | Out-Null
   Write-Host "Registered $name; verify Task Scheduler run result. Interactive user task, not a service guarantee."
 }
 'Demo' { Require-Python; if (!$Destination) {throw 'Provide a NEW isolated demo directory'}; Run-Native $Py @((Join-Path $Root 'tools\create_demo.py'),'--target',$Destination) }
 'DemoStart' {
   Require-Python; if (!$Destination) {throw 'Provide existing isolated demo directory'}
   $demo=Get-Content -LiteralPath (Join-Path $Destination 'DEMO.json') -Raw -Encoding UTF8 | ConvertFrom-Json
   if ($demo.role -ne 'sandbox') {throw 'Not a demo sandbox'}
   $auth=Join-Path $Destination 'demo-auth.json'
   if (!(Test-Path -LiteralPath $auth)) {Run-Native $Py @($Start,'set-password','--auth-file',$auth,'--port',"$Port",'--insecure-cookie')}
   Run-Native $Py @($Start,'local','--database',$demo.database,'--uuid',$demo.uuid,'--generation',"$($demo.generation)",'--writable-sandbox','--auth-file',$auth,'--port',"$Port",'--no-market-refresh')
 }
 'VerifyMarket' { Require-Python; Run-Native $Py @((Join-Path $Root 'tools\verify_market_live.py')) }
 'Maintenance' { Require-Python; if (!$MaintenanceArgs.Count -or $MaintenanceArgs[0] -notin @('init-database','set-password','restore-check','restore','takeover','takeover-rollback','upgrade-database','backup-status')) {throw 'Explicit allowed maintenance command required'}; Run-Native $Py (@($Start)+$MaintenanceArgs) }
}
