# Pure selection/lifecycle tests. Dot-sourcing does not enumerate or stop apps.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../scripts/watch_scan_memory.ps1') -FunctionsOnly
$script:checks=0
function Assert-True($Value,[string]$Message) {
    if (-not $Value) { throw $Message };$script:checks++
}
$programs=[Environment]::GetFolderPath('ProgramFiles')
Assert-True (Test-AllowlistedImage 'DellOptimizer.Systray' (Join-Path $programs 'Dell\DellOptimizer\Console\DellOptimizer.Systray.exe')) 'Known Dell image should be allowed'
Assert-True (Test-AllowlistedImage 'WidgetService' (Join-Path $programs 'WindowsApps\Microsoft.WidgetsPlatformRuntime_1_x64__publisher\WidgetService.exe')) 'Known package root should be allowed'
Assert-True (-not (Test-AllowlistedImage 'WidgetService' 'C:\Temp\WidgetService.exe')) 'Copied arbitrary executable must not qualify'
Assert-True (-not (Test-AllowlistedImage 'python' 'C:\Python\python.exe')) 'Python is never a cleanup target'
Assert-True (-not (Test-AllowlistedImage 'ZENCore' 'C:\Program Files\ZEISS\ZENCore.exe')) 'ZEN is never a cleanup target'
Assert-True (-not (Test-AllowlistedImage 'GoogleDriveFS' 'C:\Program Files\Google\Drive File Stream\GoogleDriveFS.exe')) 'Drive is never a cleanup target'
Assert-True (-not (Test-AllowlistedImage 'Widgets' (Join-Path $programs 'WindowsApps\Microsoft.WidgetsPlatformRuntime_1_x64__publisher\WidgetService.exe'))) 'Name and basename must match'
$process=[pscustomobject]@{Id=123;StartTicks=456;Name='Widgets';Path='C:\Example\Widgets.exe'}
$same=[pscustomobject]@{Id=123;StartTicks=456;Name='Widgets';Path='C:\Example\Widgets.exe'}
Assert-True (Test-SameProcess $process $same) 'Exact identity should match'
$same.StartTicks=457
Assert-True (-not (Test-SameProcess $process $same)) 'Reused PID must not match'
$now=[datetime]::UtcNow;$deadline=$now.AddHours(1)
$state=[pscustomobject]@{id='run';pid=123;state='running';hardware_closed=$false}
Assert-True ((Test-GuardLifecycle $state 'run' 123 $now $deadline $true) -eq 'running') 'Active owned worker should be monitored'
Assert-True ((Test-GuardLifecycle $state 'run' 123 $deadline $deadline $true) -eq 'deadline_reached') 'Deadline is inclusive'
Assert-True ((Test-GuardLifecycle $state 'run' 123 $now $deadline $false) -eq 'worker_exited_or_replaced') 'Gone worker stops guard'
Assert-True ((Test-GuardLifecycle $state 'other' 123 $now $deadline $true) -eq 'run_identity_changed') 'Different run must stop guard'
$state.state='stopped'
Assert-True ((Test-GuardLifecycle $state 'run' 123 $now $deadline $true) -eq 'run_not_running') 'Terminal run stops guard'
$state.state='running';$state.hardware_closed=$true
Assert-True ((Test-GuardLifecycle $state 'run' 123 $now $deadline $true) -eq 'run_not_running') 'Closed hardware stops guard'
$cooldown=@{'pid:123'=$now.AddMinutes(15)}
Assert-True (Test-Cooldown $cooldown 'pid:123' $now) 'Denied PID remains on cooldown'
Assert-True (-not (Test-Cooldown $cooldown 'pid:123' $now.AddMinutes(16))) 'Cooldown expires'
$expectedUtc=[datetime]::Parse('2026-10-11T20:03:15Z').ToUniversalTime()
Assert-True ((Convert-TrimExpiryUtc '10/11/2026 20:03:15') -eq $expectedUtc) 'Legacy UTC expiry must not shift seven hours into local time'
Assert-True ((Convert-TrimExpiryUtc '2026-10-11T20:03:15Z') -eq $expectedUtc) 'ISO UTC expiry is preserved'
Assert-True ((Convert-TrimExpiryUtc '2026-10-11T13:03:15-07:00') -eq $expectedUtc) 'Explicit timezone offset is respected'
Assert-True ((Convert-TrimExpiryUtc ([datetime]::SpecifyKind($expectedUtc,[DateTimeKind]::Unspecified))) -eq $expectedUtc) 'Untyped legacy UTC DateTime preserves expiry'
$codex=Join-Path $programs 'WindowsApps\OpenAI.Codex_26.1002.7124.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe'
Assert-True (Test-CodexRenderer 'ChatGPT' $codex ('"'+$codex+'" --type=renderer --lang=en-US')) 'Installed renderer can release resident pages'
Assert-True (-not (Test-CodexRenderer 'ChatGPT' $codex '--type=gpu-process')) 'GPU process is not a renderer'
Assert-True (-not (Test-CodexRenderer 'ChatGPT' $codex '--type=renderer-evil')) 'Renderer type must match exactly'
Assert-True (-not (Test-CodexRenderer 'ChatGPT' 'C:\Temp\ChatGPT.exe' '--type=renderer')) 'Arbitrary executable cannot qualify'
Assert-True (-not (Test-CodexRenderer 'GoogleDriveFS' $codex '--type=renderer')) 'Drive never qualifies'
Assert-True (-not (Test-CodexRenderer 'ChatGPT' ($codex.Replace('2p2nqsd0c76g0','otherpublisher')) '--type=renderer')) 'Unknown package publisher cannot qualify'
$testFolder=Join-Path ([IO.Path]::GetTempPath()) ('ff-memory-guard-'+[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($testFolder) | Out-Null
$lock=$null
$statusPath=Join-Path $testFolder 'status.json'
$lockPath=Join-Path $testFolder 'guard.lock'
try {
    Write-AtomicJson $statusPath @{state='first'}
    Write-AtomicJson $statusPath @{state='second'}
    Assert-True (((Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json).state) -eq 'second') 'Atomic status replacement must retain new complete JSON'
    $lock=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $duplicateRejected=$false
    try { $other=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);$other.Dispose() }
    catch { $duplicateRejected=$true }
    Assert-True $duplicateRejected 'Duplicate guard ownership must be refused'
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
    foreach ($path in @($statusPath,$lockPath)) { if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) } }
    [IO.Directory]::Delete($testFolder)
}
Write-Output "$script:checks offline memory-guard checks passed; no process operations performed."
