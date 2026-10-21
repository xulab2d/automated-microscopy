[CmdletBinding()]
param(
    [string]$RunDirectory,
    [int]$WorkerPid,
    [string]$WorkerStartUtc,
    [string]$DeadlineUtc,
    [ValidateRange(1,60)][int]$PollSeconds=10,
    [switch]$Once,
    [switch]$FunctionsOnly
)

# Run-scoped auxiliary-app cleanup and bounded Codex renderer page reclamation. No acquisition imports, services,
# process discovery by command text, elevated rights, or STOP/hold mutations.
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$script:AllowedNames=@('PhoneExperienceHost','WidgetService','Widgets','DellOptimizer.Systray','Dell.UUE.User.SubAgent')

function Test-AllowlistedImage([string]$Name,[string]$ImagePath) {
    if ($Name -notin $script:AllowedNames -or [string]::IsNullOrWhiteSpace($ImagePath)) { return $false }
    $full=[IO.Path]::GetFullPath($ImagePath)
    if ([IO.Path]::GetFileName($full) -ine ($Name+'.exe')) { return $false }
    $programs=[Environment]::GetFolderPath('ProgramFiles')
    switch ($Name) {
        'DellOptimizer.Systray' { return $full -ieq (Join-Path $programs 'Dell\DellOptimizer\Console\DellOptimizer.Systray.exe') }
        'Dell.UUE.User.SubAgent' { return $full -ieq (Join-Path $programs 'Dell\PlatformUserSubAgent\Dell.UUE.User.SubAgent.exe') }
        'PhoneExperienceHost' { return $full -like (Join-Path $programs 'WindowsApps\Microsoft.YourPhone_*\PhoneExperienceHost.exe') }
        default {
            return ($full -like (Join-Path $programs ('WindowsApps\Microsoft.WidgetsPlatformRuntime_*\'+$Name+'.exe'))) -or
                ($full -like (Join-Path $programs ('WindowsApps\MicrosoftWindows.Client.WebExperience_*\'+$Name+'.exe')))
        }
    }
}

function Test-SameProcess($Expected,$Observed) {
    return ($null -ne $Observed -and $Expected.Id -eq $Observed.Id -and
        $Expected.StartTicks -eq $Observed.StartTicks -and $Expected.Name -ceq $Observed.Name -and
        $Expected.Path -ieq $Observed.Path)
}

function Get-ProcessIdentity([int]$ProcessId) {
    $process=Get-Process -Id $ProcessId -ErrorAction Stop
    return [pscustomobject]@{Id=$process.Id;Name=$process.ProcessName;Path=$process.Path;
        StartTicks=$process.StartTime.ToUniversalTime().Ticks;Process=$process}
}

function Test-GuardLifecycle($State,[string]$RunId,[int]$ExpectedPid,[datetime]$Now,[datetime]$Deadline,[bool]$WorkerMatches) {
    if ($Now -ge $Deadline) { return 'deadline_reached' }
    if (-not $WorkerMatches) { return 'worker_exited_or_replaced' }
    if ($State.id -cne $RunId -or $State.pid -ne $ExpectedPid) { return 'run_identity_changed' }
    if ($State.hardware_closed -or $State.state -ne 'running') { return 'run_not_running' }
    return 'running'
}

function Test-Cooldown($Cooldown,[string]$Key,[datetime]$Now) {
    return $Cooldown.ContainsKey($Key) -and $Cooldown[$Key] -gt $Now
}

function Convert-TrimExpiryUtc($Value) {
    # Older JSON round trips stringified UTC DateTime values without their Z.
    # The ledger has always stored UTC expiry times; never reinterpret as local.
    if ($Value -is [datetime]) {
        if ($Value.Kind -eq [DateTimeKind]::Unspecified) { return [datetime]::SpecifyKind($Value,[DateTimeKind]::Utc) }
        return $Value.ToUniversalTime()
    }
    return [datetimeoffset]::Parse([string]$Value,[Globalization.CultureInfo]::InvariantCulture,
        ([Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal)).UtcDateTime
}

function Test-CodexRenderer([string]$Name,[string]$ImagePath,[string]$CommandLine) {
    if ($Name -cne 'ChatGPT' -or [string]::IsNullOrWhiteSpace($ImagePath)) { return $false }
    $base=Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'WindowsApps'
    $pattern='^'+[regex]::Escape($base)+'\\OpenAI\.Codex_[0-9.]+_x64__2p2nqsd0c76g0\\app\\ChatGPT\.exe$'
    return [IO.Path]::GetFullPath($ImagePath) -imatch $pattern -and $CommandLine -match '(?:^|\s)--type=renderer(?:\s|$)'
}

function Invoke-CodexRendererTrim($Expected,[string]$UserSid,[scriptblock]$BeforeAction) {
    $cim=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$Expected.Id)
    if ($null -eq $cim -or -not (Test-CodexRenderer $Expected.Name $Expected.Path $cim.CommandLine)) {
        throw 'Process is not an installed Codex renderer.'
    }
    $owner=Invoke-CimMethod -InputObject $cim -MethodName GetOwnerSid
    if ($owner.ReturnValue -ne 0 -or $owner.Sid -cne $UserSid) { throw 'Renderer ownership is not the current user.' }
    & $BeforeAction
    $current=Get-ProcessIdentity $Expected.Id
    if (-not (Test-SameProcess $Expected $current)) { throw 'Renderer identity changed before page reclamation.' }
    $before=$current.Process.WorkingSet64
    if ($before -lt 256MB) { return @{pid=$current.Id;state='below_minimum';before_bytes=$before} }
    Initialize-MemoryReader
    if (-not [ScanMemoryGuard.NativeMemory]::EmptyWorkingSet($current.Process.Handle)) {
        throw 'Renderer working-set reclamation failed.'
    }
    $current.Process.Refresh()
    return @{pid=$current.Id;state='trimmed';path=$current.Path;start_ticks=$current.StartTicks;
        before_bytes=$before;after_bytes=$current.Process.WorkingSet64;process_terminated=$false}
}

function Write-AtomicJson([string]$Path,$Value) {
    $temp=Join-Path ([IO.Path]::GetDirectoryName($Path)) ('.'+[guid]::NewGuid().ToString('N')+'.tmp')
    try {
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 12))
        $stream=[IO.File]::Open($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try { $stream.Write($bytes,0,$bytes.Length);$stream.Flush($true) } finally { $stream.Dispose() }
        # PS5 coerces $null to an invalid empty backup filename; NullString
        # supplies the actual null required by File.Replace's no-backup form.
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp,$Path,[NullString]::Value) } else { [IO.File]::Move($temp,$Path) }
    } finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Initialize-MemoryReader {
    if ($null -eq ('ScanMemoryGuard.NativeMemory' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace ScanMemoryGuard {
  [StructLayout(LayoutKind.Sequential)] public class MemoryStatus {
    public UInt32 length=(UInt32)Marshal.SizeOf(typeof(MemoryStatus)); public UInt32 load;
    public UInt64 total,available,pageTotal,pageAvailable,virtualTotal,virtualAvailable,extended;
  }
  public static class NativeMemory {
    [DllImport("psapi.dll",SetLastError=true)] public static extern bool EmptyWorkingSet(IntPtr process);
    [DllImport("kernel32.dll",SetLastError=true)] public static extern bool GlobalMemoryStatusEx([In,Out] MemoryStatus value);
    public static UInt64 Available() { var value=new MemoryStatus(); if(!GlobalMemoryStatusEx(value)) throw new InvalidOperationException("Memory read failed"); return value.available; }
  }
}
'@
    }
}

function Invoke-ScanMemoryGuard {
    if ($env:OS -ne 'Windows_NT') { throw 'This guard is Windows-only.' }
    if (-not $RunDirectory -or $WorkerPid -le 0 -or -not $WorkerStartUtc -or -not $DeadlineUtc) {
        throw 'RunDirectory, WorkerPid, WorkerStartUtc and DeadlineUtc are required.'
    }
    $root=(Resolve-Path -LiteralPath $RunDirectory).ProviderPath
    $runId=[IO.Path]::GetFileName($root.TrimEnd('\'))
    if ($runId -cnotmatch '^scan-\d{8}-\d{6}-[a-f0-9]{8}$') { throw 'Invalid exact run folder.' }
    $project=Split-Path (Split-Path $root -Parent) -Parent
    if ((Split-Path (Split-Path $root -Parent) -Leaf) -ne 'data' -or
        -not (Test-Path -LiteralPath (Join-Path $project '.flakefinder-project.json') -PathType Leaf)) {
        throw 'Run must belong to the managed Drive project.'
    }
    $expectedStart=[datetimeoffset]::Parse($WorkerStartUtc).UtcDateTime
    $deadline=[datetimeoffset]::Parse($DeadlineUtc).UtcDateTime
    if ($deadline -le [datetime]::UtcNow) { throw 'Deadline already passed.' }
    $requestPath=Join-Path $root 'scan-request.json'
    $requestHash=(Get-FileHash -LiteralPath $requestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $receipt=Get-Content -LiteralPath (Join-Path $root 'worker-start.json') -Raw | ConvertFrom-Json
    if ($receipt.id -cne $runId -or $receipt.pid -ne $WorkerPid -or $receipt.request_sha256 -cne $requestHash) {
        throw 'Worker receipt does not match the exact run/request.'
    }
    $worker=Get-ProcessIdentity $WorkerPid
    if ($worker.StartTicks -ne $expectedStart.Ticks -or $worker.Name -ne 'python') { throw 'Worker PID/start identity changed.' }
    $folder=Join-Path $root 'memory-guard'
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $lock=$null;$events=[Collections.Generic.List[object]]::new();$cooldown=@{};$actionCount=0;$denials=0
    $trimLedgerPath=Join-Path $project 'scan-control/renderer-trim-cooldowns.json'
    $trimLedger=@{}
    if (Test-Path -LiteralPath $trimLedgerPath) {
        $savedTrim=Get-Content -LiteralPath $trimLedgerPath -Raw | ConvertFrom-Json
        foreach ($entry in $savedTrim.PSObject.Properties) { $trimLedger[$entry.Name]=(Convert-TrimExpiryUtc $entry.Value).ToString('o') }
    }
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $state=[ordered]@{schema=1;run_id=$runId;worker_pid=$WorkerPid;worker_start_utc=$expectedStart.ToString('o');
        guard_pid=$PID;deadline_utc=$deadline.ToString('o');state='starting';threshold_bytes=[long](2.5GB);
        poll_seconds=$PollSeconds;allowed_names=$script:AllowedNames;actions=0;access_denials=0;
        renderer_trim_trigger_bytes=[long](2GB);renderer_trim_cooldown_seconds=1200;renderer_trims=0;
        automatic_restart=$false;hardware_access=$false}
    if ($Once -and (Test-Path -LiteralPath (Join-Path $folder 'status.json'))) {
        $priorState=Get-Content -LiteralPath (Join-Path $folder 'status.json') -Raw | ConvertFrom-Json
        if ($priorState.run_id -ceq $runId -and $priorState.worker_pid -eq $WorkerPid) {
            $actionCount=[int]$priorState.actions;$denials=[int]$priorState.access_denials
            $state.renderer_trims=[int]$priorState.renderer_trims
        }
        if (Test-Path -LiteralPath (Join-Path $folder 'events.json')) {
            foreach ($event in (Get-Content -LiteralPath (Join-Path $folder 'events.json') -Raw | ConvertFrom-Json)) { $events.Add($event) }
        }
    }
    function Save-GuardState {
        $state.updated_at=[datetime]::UtcNow.ToString('o');$state.actions=$actionCount;$state.access_denials=$denials
        Write-AtomicJson (Join-Path $folder 'status.json') $state
    }
    function Add-GuardEvent([string]$Kind,$Detail) {
        $events.Add([ordered]@{at=[datetime]::UtcNow.ToString('o');kind=$Kind;detail=$Detail})
        while ($events.Count -gt 200) { $events.RemoveAt(0) }
        Write-AtomicJson (Join-Path $folder 'events.json') @($events.ToArray())
    }
    function Read-Lifecycle {
        if ([datetime]::UtcNow -ge $deadline) { return 'deadline_reached' }
        try { $current=Get-ProcessIdentity $WorkerPid } catch { return 'worker_exited_or_unreadable' }
        if (-not (Test-SameProcess $worker $current)) { return 'worker_exited_or_replaced' }
        $saved=Get-Content -LiteralPath (Join-Path $root 'scan-status.json') -Raw | ConvertFrom-Json
        if ($saved.request_sha256 -cne $requestHash) { return 'run_request_identity_changed' }
        return Test-GuardLifecycle $saved $runId $WorkerPid ([datetime]::UtcNow) $deadline (Test-SameProcess $worker $current)
    }
    try {
        # File existence is not ownership; this exclusive OS handle is the lock.
        $lock=[IO.File]::Open((Join-Path $folder 'guard.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        Initialize-MemoryReader
        $unreadable=0
        while ($true) {
            try { $life=Read-Lifecycle;$unreadable=0 } catch {
                $unreadable++;$state.state='waiting_for_readable_status';$state.last_error=$_.Exception.Message
                Save-GuardState
                if ($unreadable -ge 12) { throw 'Run status remained unreadable; no processes were stopped.' }
                Start-Sleep -Seconds $PollSeconds;continue
            }
            if ($life -ne 'running') { $state.state=$life;Save-GuardState;break }
            $now=[datetime]::UtcNow
            foreach ($key in @($cooldown.Keys)) { if ($cooldown[$key] -le $now) { $cooldown.Remove($key) } }
            $available=[ScanMemoryGuard.NativeMemory]::Available();$state.available_bytes=$available
            $state.state=if ($available -lt 2.5GB) {'low_memory'} else {'monitoring'}
            $state.cooldown_entries=$cooldown.Count;Save-GuardState
            if ($available -lt 2.5GB) {
                $candidates=@(Get-Process -Name $script:AllowedNames -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending)
                $thisTick=0
                foreach ($candidate in $candidates) {
                    if ($thisTick -ge 3 -or [ScanMemoryGuard.NativeMemory]::Available() -ge 2.5GB) { break }
                    $identityKey='pid:'+ $candidate.Id
                    if ((Test-Cooldown $cooldown $identityKey $now) -or (Test-Cooldown $cooldown ('name:'+$candidate.ProcessName) $now)) { continue }
                    try {
                        $expected=Get-ProcessIdentity $candidate.Id
                        if (-not (Test-AllowlistedImage $expected.Name $expected.Path)) { throw 'Name/executable path is outside the exact allowlist.' }
                        $cim=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$expected.Id)
                        $owner=Invoke-CimMethod -InputObject $cim -MethodName GetOwnerSid
                        if ($owner.ReturnValue -ne 0 -or $owner.Sid -cne $identity) { throw 'Same-user process ownership could not be established.' }
                        if ((Read-Lifecycle) -ne 'running') { break }
                        # Save intent first; logging failure stops the guard before termination.
                        Add-GuardEvent 'stop_intent' @{pid=$expected.Id;name=$expected.Name;path=$expected.Path;start_ticks=$expected.StartTicks}
                        $current=Get-ProcessIdentity $expected.Id
                        if (-not (Test-SameProcess $expected $current) -or -not (Test-AllowlistedImage $current.Name $current.Path)) {
                            throw 'PID/start/executable changed before stop.'
                        }
                        Stop-Process -InputObject $current.Process -Confirm:$false -ErrorAction Stop
                        $actionCount++;$thisTick++;$cooldown['name:'+$current.Name]=[datetime]::UtcNow.AddSeconds(60)
                        Add-GuardEvent 'stopped' @{pid=$current.Id;name=$current.Name;restart_cooldown_seconds=60}
                        Save-GuardState;Start-Sleep -Milliseconds 300
                    } catch {
                        $denials++;$cooldown[$identityKey]=[datetime]::UtcNow.AddMinutes(15)
                        Add-GuardEvent 'denied_or_identity_unverified' @{pid=$candidate.Id;name=$candidate.ProcessName;error=$_.Exception.Message;cooldown_seconds=900}
                        Save-GuardState
                    }
                }
            }
            # Release resident renderer pages, preserving the app and all unsaved state.
            # Pressure-only, at most three per pass, twenty minutes per exact PID/start
            # across continuations. Never trim acquisition, ZEN, Drive or OS processes.
            if ([ScanMemoryGuard.NativeMemory]::Available() -lt 2GB) {
                $renderers=@(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | Where-Object {$_.WorkingSet64 -ge 256MB} | Sort-Object WorkingSet64 -Descending)
                $trimmed=0
                foreach ($renderer in $renderers) {
                    if ($trimmed -ge 3 -or [ScanMemoryGuard.NativeMemory]::Available() -ge 2.5GB) { break }
                    $trimKey=$null
                    try {
                        $expected=Get-ProcessIdentity $renderer.Id
                        $trimKey=('{0}:{1}' -f $expected.Id,$expected.StartTicks)
                        if ($trimLedger.ContainsKey($trimKey) -and (Convert-TrimExpiryUtc $trimLedger[$trimKey]) -gt [datetime]::UtcNow) { continue }
                        $trimLedger[$trimKey]=[datetime]::UtcNow.AddMinutes(20).ToString('o')
                        Write-AtomicJson $trimLedgerPath $trimLedger
                        $detail=Invoke-CodexRendererTrim $expected $identity {
                            if ((Read-Lifecycle) -ne 'running') { throw 'Worker no longer active; no page reclamation.' }
                            Add-GuardEvent 'renderer_trim_intent' @{pid=$expected.Id;path=$expected.Path;start_ticks=$expected.StartTicks}
                        }
                        Add-GuardEvent 'renderer_page_reclamation' $detail
                        if ($detail.state -eq 'trimmed') { $trimmed++;$state.renderer_trims++ }
                        Save-GuardState
                    } catch {
                        Add-GuardEvent 'renderer_trim_skipped' @{pid=$renderer.Id;error=$_.Exception.Message;cooldown_seconds=1200}
                    }
                }
            }
            if ($Once) { break }
            Start-Sleep -Seconds $PollSeconds
        }
    } catch {
        # A second instance must not overwrite the owner's status.
        if ($null -ne $lock) { $state.state='failed';$state.last_error=$_.Exception.Message;try { Save-GuardState } catch {} }
        throw
    } finally { if ($null -ne $lock) { $lock.Dispose() } }
}

if (-not $FunctionsOnly) { Invoke-ScanMemoryGuard }
