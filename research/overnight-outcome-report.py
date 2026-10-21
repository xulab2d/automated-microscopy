"""Summarize this ended campaign from saved files; no hardware access."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import collections
import json
import shutil

root = Path('data/whole-chip-standardized-01')
batch = json.loads((root / 'batch.json').read_text())
assert batch['state'] == 'failed' and 'storage reserve exhausted' in batch['error']
exit_record = json.loads((root / 'acquisition-exit-verification.json').read_text())
assert not any(exit_record['prior_processes_alive'].values())
chips = []
for path in sorted(root.glob('chip*/manifest.json')):
    manifest = json.loads(path.read_text())
    records = manifest.get('completed', {})
    images = [path.parent / 'tiles' / (tile + '.tiff') for tile in records]
    chips.append({
        'chip': path.parent.name,
        'completed_tiles': len(records),
        'scientific_tiffs_present': sum(image.is_file() for image in images),
        'scientific_tiff_bytes': sum(image.stat().st_size for image in images if image.is_file()),
        **{key: manifest.get(key) for key in (
            'planned_area_acquired', 'chip_extent_validated', 'focus_coverage_complete', 'state')},
        'focus_limited_records': sum(bool(record.get('focus_limited')) for record in records.values()),
    })
ended = datetime.fromtimestamp(batch['updated_at'], timezone.utc)
# The authorized cutoff, 09:00 local = 16:00 UTC, establishes this date's offset.
local = ended.astimezone(timezone(timedelta(hours=-7), 'PDT'))
seed_counts = dict(collections.Counter(record['classification'] for record in batch['seed_records']))
attempt_counts = dict(collections.Counter(record['state'] for record in batch['boundary_attempts']))
limitations = [
    'Chip002 northern tip extends past the observed raster perimeter; its 511 planned fields do not establish complete chip extent.',
    'Chip001 tilt is about 2.43 degrees, giving about 16.2 micrometres height variation across a camera field. Plane-supported field centres do not mean every pixel is sharp.',
    f"{attempt_counts.get('unresolved', 0)} unresolved boundary attempts include repeat observations of physical chips; these are not distinct chip counts.",
    f"{seed_counts.get('uncertain', 0)} uncertain seed records and {batch['remaining_lattice_probes']} remaining lattice probes prevent any all-chips-found or absence claim.",
    'Broad optical features include residue and dust. No validated thin MoTe2 or monolayer identification.',
]
report = {
    'schema': 1,
    'recorded_at': datetime.now(timezone.utc).isoformat(),
    'acquisition_ended_utc': ended.isoformat(),
    'acquisition_ended_local': local.isoformat(),
    'outcome': 'stopped_at_capture_storage_reserve',
    'saved_terminal_state': batch['state'],
    'terminal_error': batch['error'],
    'restart_authorized': False,
    'coordinate_frame_id': batch['coordinate_frame_id'],
    'chips': chips,
    'planned_raster_images_retained': sum(chip['completed_tiles'] for chip in chips),
    'search_frames_retained': len(list((root / 'search/frames').glob('*.tiff'))),
    'boundary_attempt_states': attempt_counts,
    'search_record_classifications': seed_counts,
    'remaining_lattice_probes': batch['remaining_lattice_probes'],
    'all_chips_found': False,
    'free_disk_gib_at_report': round(shutil.disk_usage(root).free / 2**30, 3),
    'authorized_cutoff_utc': '2026-10-09T16:00:00Z',
    'stop_reason_is_storage_not_cutoff': True,
    'limitations': limitations,
    'analysis': 'Existing offline worker continues on committed chips without microscope access. Chip001 supplement retains 31 recovered fields and 505 source masks; original results remain unchanged.',
    'acquisition_exit_verification': 'acquisition-exit-verification.json',
    'heartbeat_status': 'PAUSED',
    'gallery_url': 'http://127.0.0.1:8766/gallery',
    'data_policy': 'No scientific frames, sidecars, acquisition manifests or original analysis shards deleted or overwritten by supervision.',
}
(root / 'overnight-outcome.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
lines = [
    'Overnight acquisition outcome',
    f'Ended {local:%Y-%m-%d %H:%M:%S %Z}: capture storage reserve reached.',
    'Coordinator, scanner and cutoff guard exited; filesystem hardware lease released.',
    'No automatic restart. Overnight heartbeat paused.', '', 'Retained raster coverage:',
]
for chip in chips:
    lines.append(f"{chip['chip']}: {chip['completed_tiles']} scientific TIFFs; planned area acquired={chip['planned_area_acquired']}; chip extent verified={chip['chip_extent_validated']}.")
lines += [
    '', f"Total: {report['planned_raster_images_retained']} raster images and {report['search_frames_retained']} search TIFFs, plus boundary/autofocus/calibration evidence.",
    'Scientific frames, acquisition metadata and prior analysis records are preserved.',
    '', 'Coverage and focus limits:', *['- ' + item for item in limitations],
    '', report['analysis'], 'Full record: overnight-outcome.json', 'Gallery: ' + report['gallery_url'],
]
(root / 'overnight-outcome.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
print(json.dumps({key: report[key] for key in (
    'acquisition_ended_local', 'chips', 'planned_raster_images_retained',
    'search_frames_retained', 'free_disk_gib_at_report')}, indent=2))
