"""Replay saved chip-edge focus selection without importing a hardware client."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from flakefinder.full_chip import edge_focus_roi
from flakefinder.imaging import load_frame, preview
from flakefinder.model import AcquisitionError, CameraTransform, atomic_json


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('discovery', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--probes', nargs='+', type=int, default=[9, 10, 11])
    args = parser.parse_args()
    saved = json.loads(args.discovery.read_text(encoding='utf-8'))
    transform = CameraTransform(**saved['transform'])
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        'schema': 1, 'hardware_access': False,
        'discovery_path': str(args.discovery), 'discovery_sha256': sha256(args.discovery),
        'qualification': 'Offline ROI selection only. No focus measurement or chip-boundary acceptance is inferred.',
        'records': [], 'source_sha256': {
            name: sha256(SOURCE / name) for name in (
                'flakefinder/full_chip.py', 'flakefinder/sparse_acquisition.py',
                'tests/test_edge_focus_anchors.py', 'tests/test_sparse_edge_focus.py',
                'scripts/replay_edge_focus_guidance.py')},
    }
    for index in args.probes:
        relative = f'frames/probe_{index:06d}.tiff'
        record = next(r for r in saved['records'] if r.get('image') == relative)
        path = args.discovery.parent / relative
        meta_path = path.with_suffix('.json')
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        frame = load_frame(path)
        rgb = frame.rgb
        result = {'image': str(path), 'image_sha256': sha256(path),
                  'metadata_sha256': sha256(meta_path), 'historical_edge_accepted': record.get('edge') is not None}
        raw_hash = hashlib.sha256(rgb.tobytes()).hexdigest()
        expected = meta['metadata'].get('zen_vendor_result', {}).get('raw_rgb_sha256')
        result['raw_pixels_sha256'] = raw_hash
        result['vendor_pixels_match'] = expected == raw_hash if expected else None
        if expected and expected != raw_hash:
            raise ValueError(f'Raw pixels do not match capture receipt: {path}')
        try:
            result['default_selection'] = edge_focus_roi(frame, 64., transform)
        except AcquisitionError as exc:
            result['default_rejection'] = str(exc)
        proposal = record.get('edge_validation', {}).get('original_label_proposal')
        if proposal:
            result['hint'] = {key: proposal[key] for key in ('normal', 'offset_um')}
            start = time.perf_counter()
            try:
                result['guided_selection'] = edge_focus_roi(frame, 64., transform, boundary_proposal=proposal)
            except AcquisitionError as exc:
                result['guided_rejection'] = str(exc)
            result['selection_ms'] = (time.perf_counter() - start) * 1000
        else:
            result['guided_rejection'] = 'No current label-side-supported proposal; no guided sweep eligible.'
        selection = result.get('guided_selection')
        if selection:
            # Display conversion only; selection above used unchanged raw pixels.
            white = float(meta['metadata'].get('white_level', np.iinfo(rgb.dtype).max))
            view = preview(rgb, white)
            x0, y0, x1, y1 = selection['box_px']
            crop = cv2.resize(view[y0:y1, x0:x1], (512, 512), interpolation=cv2.INTER_AREA)
            h, w = view.shape[:2]
            small = cv2.resize(view, (768, round(h * 768 / w)), interpolation=cv2.INTER_AREA)
            cv2.rectangle(small, (round(x0 * 768 / w), round(y0 * 768 / w)),
                          (round(x1 * 768 / w), round(y1 * 768 / w)), (255, 220, 30), 2)
            canvas = np.full((max(small.shape[0], 512) + 48, 1296, 3), 22, np.uint8)
            canvas[48:48 + small.shape[0], :768] = small
            canvas[48:560, 784:1296] = crop
            cv2.putText(canvas, f'Probe {index:06d} | 64 um autofocus ROI | saved raw frame',
                        (12, 30), cv2.FONT_HERSHEY_SIMPLEX, .65, (230, 230, 230), 1, cv2.LINE_AA)
            preview_path = args.output / f'probe_{index:06d}_roi.jpg'
            if not cv2.imwrite(str(preview_path), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 94]):
                raise OSError(f'Cannot save preview: {preview_path}')
            result['preview'] = str(preview_path)
        report['records'].append(result)
        atomic_json(args.output / 'replay.json', report)
    print(json.dumps([{key: value for key, value in r.items() if key in (
        'image', 'default_rejection', 'guided_rejection', 'guided_selection', 'selection_ms', 'vendor_pixels_match')}
        for r in report['records']], indent=2))


if __name__ == '__main__':
    main()
