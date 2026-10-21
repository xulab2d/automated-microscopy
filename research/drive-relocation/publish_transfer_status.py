"""Publish small user-visible transfer status only at meaningful milestones."""
from pathlib import Path
import json

import bounded_transfer as transfer


def publish_if_changed(info, journal_folder, live, force=False):
    journal_folder = Path(journal_folder)
    checkpoint = journal_folder/'published-status.json'
    previous = transfer.read_json(checkpoint) if checkpoint.exists() else {}
    counts = live['progress']
    copied = sum(counts.get(state, {}).get('bytes', 0) for state in ('mounted_copy_verified', 'content_matched', 'client_confirmed'))
    matched = sum(counts.get(state, {}).get('bytes', 0) for state in ('content_matched', 'client_confirmed'))
    confirmed = counts.get('client_confirmed', {}).get('bytes', 0)
    terminal = live['state'] in ('complete', 'complete_refresh_required', 'blocked_disk_reserve', 'blocked_upload_confirmation', 'failed', 'stopped')
    if not (force or not previous or terminal or confirmed > previous.get('confirmed_bytes', 0)
            or matched-previous.get('matched_bytes', 0) >= 256*transfer.MIB):
        return False
    retired=counts.get('retired_intentionally',{'files':0,'bytes':0})
    total = sum(item['bytes'] for item in counts.values())-retired['bytes']
    total_files = sum(item['files'] for item in counts.values())-retired['files']
    remaining = sum(counts.get(state, {}).get('bytes', 0) for state in ('pending', 'source_changed', 'source_missing'))
    status = {'schema': 1, 'updated_at_utc': transfer.probe.now(), 'worker_heartbeat_utc': live['updated_at_utc'],
        'state': live['state'], 'worker_pid': live['pid'], 'project_mirror_files': total_files,
        'project_mirror_bytes': total, 'copied_and_readback_bytes': copied,
        'two_snapshot_content_matched_bytes': matched, 'strong_client_confirmed_bytes': confirmed,
        'remaining_to_copy_bytes': remaining, 'file_states': counts,
        'all_source_originals_retained':live.get('all_source_originals_retained',True),
        'local_copies_offloaded':live.get('local_copies_offloaded',{'files':0,'bytes':0}),
        'intentionally_retired_intermediates':retired,
        'all_cloud_copies_preserved':not bool(retired['files']),'retained_scope_cloud_copies_preserved':True,'current_final_scientific_inputs_preserved':True,
        'content_match_is_not_final_global_sync_confirmation': True,
        'ready_laptop_zip': {'name': 'Laptop Review.zip', 'bytes': 1071927557,
            'sha256': '906f58f1a180a8df4e2977a11dc973495e14a1704051fee82882da7c095c1ae5',
            'client_confirmed': True, 'url': 'https://drive.google.com/file/d/1JeiYUOTpL2NFjLbZj6HCRNN79lUV10Rc/view'},
        'exclusions': 'Named exclusions: virtual environments, Git internals, caches, dot-prefixed .test-* directories, credentials and live relocation journals. Ordinary data/test-* outputs remain included (28,792 files / approximately 265.2 MB in the original inventory). The separately approved 5,896 high-resolution vendor/routing intermediates are intentionally discarded under the hash-pinned retirement manifest. Final scientific TIFFs, analysis masks/labels, focus anchors, calibration, previews, metadata, research, code and dependency declarations remain included. Edited files receive version-preserving refresh.'}
    destination = transfer.ensure_destination(Path(info['destination_root']))
    transfer.write_json(destination/'TRANSFER-STATUS.json', status)
    text = '\n'.join([
        'AUTOMATED MICROSCOPY — DRIVE TRANSFER',
        'Updated UTC: '+status['updated_at_utc'],
        'Worker state: '+live['state'],
        '',
        'READY: Laptop Review.zip (1,071,927,557 bytes), checksum and upload verified.',
        status['ready_laptop_zip']['url'],
        '',
        f'Project mirror: {total_files:,} files / {total/1e9:.2f} GB tracked.',
        f'Copied and read back: {copied/1e9:.2f} GB.',
        f'Owned-file checksums matched in two Drive snapshots: {matched/1e9:.2f} GB.',
        f'Final client upload audit (also required an empty queue): {confirmed/1e9:.2f} GB.',
        f'Remaining to copy or refresh: {remaining/1e9:.2f} GB.',
        f'Intentionally discarded intermediates (not counted as uploaded): {retired["files"]:,} files / {retired["bytes"]/1e9:.2f} GB. Separate exact retirement receipts are retained.',
        '',
        (f"Verified local copies offloaded: {status['local_copies_offloaded']['files']:,} files / {status['local_copies_offloaded']['bytes']/1e9:.2f} GB. Their cloud copies and current final scientific inputs remain preserved. Per-file proofs are retained."
         if status['local_copies_offloaded']['files'] else 'Scientific source copies remain on the microscope computer. No cache deletion is performed.'),
        'The worker continues while unrelated analysis uploads run; content matches are not mislabeled as final global sync completion.',
        'This status updates at meaningful verification milestones, not every few seconds.',
        '',
        'Exclusions: '+status['exclusions'],
        ''
    ])
    temporary = destination/'TRANSFER-STATUS.txt.pending'
    temporary.write_text(text, encoding='utf-8')
    # The explicitly maintained status file is the only text destination replaced here.
    temporary.replace(destination/'TRANSFER-STATUS.txt')
    transfer.write_json(checkpoint, {'matched_bytes': matched, 'confirmed_bytes': confirmed,
        'published_at_utc': status['updated_at_utc']})
    return True
