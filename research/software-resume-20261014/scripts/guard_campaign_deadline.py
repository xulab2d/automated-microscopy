"""Create one cooperative campaign STOP at an explicit UTC deadline.

No hardware client, process termination, budget edit, or acquisition restart.
Budget pauses remain guarded because a separate authorized owner may extend
them. Completion, failure, operator STOP and coordinate changes end this guard.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
import os
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from flakefinder.lease import HardwareLease
from flakefinder.model import atomic_json
from scripts.run_overnight_campaign import keep_system_awake

TERMINAL_STATES = {'search_sampling_complete', 'complete', 'failed', 'stopped'}
BUDGET_STATES = {'time_budget_reached', 'chip_budget_reached', 'storage_budget_reached'}


def parse_utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use an explicit UTC date, e.g. 2026-10-09T16:00:00Z.') from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError('Deadline requires UTC Z or +00:00, not a local or unspecified timezone.')
    return parsed.astimezone(timezone.utc)


def create_stop(root, coordinate_frame_id, deadline):
    message = json.dumps({'reason': 'Explicit acquisition deadline reached; cooperative stop requested.',
                          'coordinate_frame_id': coordinate_frame_id,
                          'deadline_utc': deadline.isoformat(), 'requested_at_utc': datetime.now(timezone.utc).isoformat()})
    try:
        with (root / 'STOP').open('x', encoding='utf-8') as stream:
            stream.write(message + '\n')
        return True
    except FileExistsError:
        return False  # An operator or the coordinator already stopped it.


def guard(root, coordinate_frame_id, deadline, *, poll_seconds=5, clock=time.time, sleep=time.sleep):
    root = Path(root).resolve()
    if not coordinate_frame_id or poll_seconds <= 0:
        raise ValueError('Need a pinned coordinate frame and positive polling interval.')
    if deadline.tzinfo is None or deadline.utcoffset() != timedelta(0):
        raise ValueError('Deadline must be timezone-aware UTC.')
    first = json.loads((root / 'batch.json').read_text(encoding='utf-8'))
    if first.get('coordinate_frame_id') != coordinate_frame_id:
        raise ValueError('Campaign coordinate frame differs from the requested guard target.')
    directory_identity = (root.stat().st_dev, root.stat().st_ino)
    lease = HardwareLease('deadline-guard:' + os.path.normcase(str(root)), root / 'control-locks')
    lease.acquire()  # Guard-process lock only; never the microscope lease.
    report = {'schema': 1, 'pid': os.getpid(), 'root': str(root),
              'coordinate_frame_id': coordinate_frame_id, 'deadline_utc': deadline.isoformat(),
              'started_at': clock(), 'state': 'guarding', 'stop_created': False,
              'policy': 'Exclusive root STOP at deadline, including during a raster; no kill/restart/budget edit.',
              'timing_note': 'Cooperative request; physical completion depends on the acquisition owner observing STOP.'}

    def save(**updates):
        report.update(**updates, updated_at=clock())
        try:
            atomic_json(root / 'deadline-guard.json', report)
        except OSError as exc:
            # A reporting failure must not silently disarm deadline enforcement.
            print('Deadline status write failed: ' + str(exc), file=sys.stderr, flush=True)

    awake = False
    try:
        awake = keep_system_awake(True)
        report['idle_sleep_inhibited'] = awake
        while True:
            stat = root.stat()
            if root.resolve() != root or (stat.st_dev, stat.st_ino) != directory_identity:
                save(state='identity_changed', reason='Campaign directory was replaced; no STOP written.')
                return 2
            batch = None
            try:
                batch = json.loads((root / 'batch.json').read_text(encoding='utf-8'))
                if not isinstance(batch, dict):
                    batch = None
                    raise ValueError('Campaign status must be a JSON object.')
                if batch.get('coordinate_frame_id') != coordinate_frame_id:
                    save(state='identity_changed', reason='Campaign coordinate frame changed; no STOP written.')
                    return 2
                report['identity_last_verified_at'] = clock()
                report.pop('status_read_error', None)
            except (OSError, ValueError) as exc:
                report['status_read_error'] = str(exc)
            if (root / 'STOP').exists():
                save(state='stop_already_present', reason='Existing STOP preserved; guard finished.')
                return 0
            if batch is not None and batch.get('state') in TERMINAL_STATES:
                save(state='campaign_terminal', campaign_state=batch.get('state'))
                return 0
            remaining = deadline.timestamp() - clock()
            if remaining <= 0:
                # Missing/corrupt progress cannot excuse running past cutoff.
                # Only the original pinned directory is targeted; any observed
                # coordinate-frame change above prevents touching its STOP.
                created = create_stop(root, coordinate_frame_id, deadline)
                save(state='deadline_stop_requested' if created else 'stop_already_present',
                     stop_created=created, deadline_observed_at=clock(),
                     campaign_state=batch.get('state') if batch is not None else 'unreadable')
                return 0
            state = batch.get('state') if batch is not None else 'unreadable'
            save(state='waiting_at_budget_pause' if state in BUDGET_STATES else 'guarding',
                 campaign_state=state, current_chip=batch.get('current_chip') if batch else None,
                 remaining_seconds=remaining)
            sleep(min(poll_seconds, remaining))
    finally:
        try:
            if awake:
                keep_system_awake(False)
                save(idle_sleep_inhibited=False)
        finally:
            lease.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--coordinate-frame-id', required=True)
    parser.add_argument('--deadline-utc', type=parse_utc, required=True)
    args = parser.parse_args()
    return guard(args.output, args.coordinate_frame_id, args.deadline_utc)


if __name__ == '__main__':
    sys.exit(main())
