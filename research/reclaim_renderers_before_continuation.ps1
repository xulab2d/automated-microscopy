$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'software-next-20261009\scripts\watch_scan_memory.ps1') -FunctionsOnly
$taskProject='G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy'
$taskRun=Join-Path $taskProject 'data\scan-20261011-130111-bf685ad5'
$taskStatus=Get-Content -LiteralPath (Join-Path $taskRun 'scan-status.json') -Raw | ConvertFrom-Json
if (-not $taskStatus.hardware_closed -or $taskStatus.state -ne 'failed') { throw 'Expected closed failed parent.' }
foreach ($taskProcessId in @(8492,32252,26460,22432)) {
    if (Get-Process -Id $taskProcessId -ErrorAction SilentlyContinue) { throw 'Prior worker/helper still present.' }
}
$taskLedgerPath=Join-Path $taskProject 'scan-control\renderer-trim-cooldowns.json'
$taskLedger=@{}
$taskSaved=Get-Content -LiteralPath $taskLedgerPath -Raw | ConvertFrom-Json
foreach ($taskProperty in $taskSaved.PSObject.Properties) { $taskLedger[$taskProperty.Name]=(Convert-TrimExpiryUtc $taskProperty.Value).ToString('o') }
$taskUser=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Initialize-MemoryReader
$taskBefore=[ScanMemoryGuard.NativeMemory]::Available()
$taskActions=@();$taskCount=0
if ($taskBefore -lt 2.5GB) {
  foreach ($taskRenderer in @(Get-Process -Name ChatGPT | Where-Object {$_.WorkingSet64 -ge 256MB} | Sort-Object WorkingSet64 -Descending)) {
    if ($taskCount -ge 3 -or [ScanMemoryGuard.NativeMemory]::Available() -ge 2.5GB) { break }
    $taskExpected=Get-ProcessIdentity $taskRenderer.Id
    $taskCim=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$taskExpected.Id)
    if (-not (Test-CodexRenderer $taskExpected.Name $taskExpected.Path $taskCim.CommandLine)) { continue }
    $taskKey=('{0}:{1}' -f $taskExpected.Id,$taskExpected.StartTicks)
    if ($taskLedger.ContainsKey($taskKey) -and (Convert-TrimExpiryUtc $taskLedger[$taskKey]) -gt [datetime]::UtcNow) { continue }
    $taskLedger[$taskKey]=[datetime]::UtcNow.AddMinutes(20).ToString('o')
    Write-AtomicJson $taskLedgerPath $taskLedger
    $taskDetail=Invoke-CodexRendererTrim $taskExpected $taskUser {
        $taskFresh=Get-Content -LiteralPath (Join-Path $taskRun 'scan-status.json') -Raw | ConvertFrom-Json
        if (-not $taskFresh.hardware_closed) { throw 'Closure changed.' }
    }
    $taskActions+=$taskDetail;$taskCount++
  }
}
$taskReceipt=@{at=[datetime]::UtcNow.ToString('o');before_bytes=$taskBefore;after_bytes=[ScanMemoryGuard.NativeMemory]::Available();actions=$taskActions;policy='One pressure-only pre-launch pass; verified same-user installed renderers, maximum three, persisted20-minute cooldown; no process termination.'}
Write-AtomicJson (Join-Path $taskProject 'research\session-20261011-progress-continuation\prelaunch-memory-relief.json') $taskReceipt
$taskReceipt | ConvertTo-Json -Depth 7
