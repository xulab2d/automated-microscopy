$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'software-next-20261009\scripts\watch_scan_memory.ps1') -FunctionsOnly
$taskProject='G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy'
$taskUser=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskRows=@()
Initialize-MemoryReader
$taskBefore=[ScanMemoryGuard.NativeMemory]::Available()
foreach ($taskApp in @(Get-Process -Name PhoneExperienceHost,Widgets,WidgetService,DellOptimizer.Systray,Dell.UUE.User.SubAgent -ErrorAction SilentlyContinue)) {
    $taskExpected=Get-ProcessIdentity $taskApp.Id
    if (-not (Test-AllowlistedImage $taskExpected.Name $taskExpected.Path)) { continue }
    $taskCim=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$taskExpected.Id)
    $taskOwner=Invoke-CimMethod -InputObject $taskCim -MethodName GetOwnerSid
    if ($taskOwner.ReturnValue -ne 0 -or $taskOwner.Sid -cne $taskUser) { continue }
    $taskCurrent=Get-ProcessIdentity $taskExpected.Id
    if (-not (Test-SameProcess $taskExpected $taskCurrent)) { throw 'Utility identity changed.' }
    $taskRow=@{pid=$taskCurrent.Id;path=$taskCurrent.Path;name=$taskCurrent.Name;start_ticks=$taskCurrent.StartTicks;state='stop_intent'}
    $taskRows+=$taskRow
    Write-AtomicJson (Join-Path $taskProject 'research\session-20261011-progress-continuation\utility-relief.json') @{actions=$taskRows;before_bytes=$taskBefore}
    Stop-Process -InputObject $taskCurrent.Process -Confirm:$false
    $taskRow.state='stopped'
}
$taskResult=@{at=[datetime]::UtcNow.ToString('o');before_bytes=$taskBefore;after_bytes=[ScanMemoryGuard.NativeMemory]::Available();actions=$taskRows}
Write-AtomicJson (Join-Path $taskProject 'research\session-20261011-progress-continuation\utility-relief.json') $taskResult
$taskResult | ConvertTo-Json -Depth 7
