$ErrorActionPreference = 'Stop'
$targetParentId = 27528
$parentRecord = Get-CimInstance Win32_Process -Filter "ProcessId=$targetParentId"
if ($parentRecord.CommandLine -notmatch 'analyze_graphene_run.py' -or $parentRecord.CommandLine -notmatch 'operator-empirical-full-v1') { throw 'Parent command does not match the offline analysis.' }
$parentProcess = Get-Process -Id $targetParentId
$parentStarted = $parentProcess.StartTime
$childRecords = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$targetParentId" | Sort-Object ProcessId)
if ($childRecords.Count -ne 2) { throw 'Expected exactly two bounded field children.' }
$assignments = @()
$targetMasks = @(1,4)
for ($index = 0; $index -lt 2; $index++) {
    $record = $childRecords[$index]
    if ($record.CommandLine -notmatch 'multiprocessing.spawn' -or $record.CommandLine -notmatch 'parent_pid=27528') { throw 'Unexpected child command.' }
    $process = Get-Process -Id $record.ProcessId
    $assignments += @{pid=$record.ProcessId;started=$process.StartTime;mask=$targetMasks[$index];command_line=$record.CommandLine;initial_mask=$process.ProcessorAffinity.ToInt64()}
}
$evidence = @{schema=1;parent_pid=$targetParentId;parent_started=$parentStarted;parent_command_line=$parentRecord.CommandLine;assignments=$assignments;started_at=(Get-Date -Format o);state='running';corrections=0;policy='Runtime affinity only; original immutable code requested logical CPU0/1. Root independently measured Windows topology: CPU0/1 share a core, CPU2/3 share another. Enforce CPU0 andCPU2 on the two exact verified field workers. No scientific/source changes.'}
$evidencePath = Join-Path $PSScriptRoot 'empirical-runtime-affinity.json'
$lastSaved = Get-Date
$evidence | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $evidencePath -Encoding utf8
try {
    while ($true) {
        $parentProcess = Get-Process -Id $targetParentId -ErrorAction SilentlyContinue
        if ($null -eq $parentProcess -or $parentProcess.StartTime -ne $parentStarted) { break }
        foreach ($entry in $assignments) {
            $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
            if ($null -eq $process -or $process.StartTime -ne $entry.started) { continue }
            if ($process.ProcessorAffinity.ToInt64() -ne $entry.mask) {
                $process.ProcessorAffinity = [IntPtr]$entry.mask
                $evidence.corrections++
            }
        }
        if (((Get-Date)-$lastSaved).TotalSeconds -ge 30) {
            $evidence.updated_at=Get-Date -Format o
            $evidence | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $evidencePath -Encoding utf8
            $lastSaved=Get-Date
        }
        Start-Sleep -Milliseconds 250
    }
    $evidence.state='complete_parent_exited'
} catch { $evidence.state='failed';$evidence.error=$_.Exception.Message;throw }
finally { $evidence.finished_at=Get-Date -Format o;$evidence | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $evidencePath -Encoding utf8 }
