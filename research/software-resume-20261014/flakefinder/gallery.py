"""Read-only, lazy field gallery. No acquisition or detector code is imported."""
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from urllib.parse import quote
import json
from .gallery_supplement import load_supplements


def confined(root, path):
    path = Path(path)
    target = (path if path.is_absolute() else root / path).resolve()
    return target if target.is_relative_to(root) else None


def file_url(root, path):
    target = confined(root, path)
    return '/files/' + quote(target.relative_to(root).as_posix(), safe='/')+'?v='+str(target.stat().st_mtime_ns) if target is not None and target.is_file() else None


def analysis_failures(summary):
    """Latest recorded field failures, excluding successfully retried fields."""
    successful = {t.get('tile_id') or str(t.get('key', '')).rsplit('/', 1)[-1]
                  for t in summary.get('tiles', [])}
    failures = {}
    for item in summary.get('failed_tiles', []):
        if not isinstance(item, dict):
            continue
        key = item.get('tile_id') or item.get('tile_key') or item.get('key')
        if not key:
            continue
        tile_id = str(key).rsplit('/', 1)[-1]
        if tile_id not in successful:
            failures[tile_id] = str(item.get('error') or 'Analysis failed; no error detail was recorded.')
    return failures


class FieldGallery:
    """Cache immutable JSON snapshots; acquisition/analysis replace files atomically."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = OrderedDict()
        self.lock = RLock()

    def read(self, path, default=None):
        target = confined(self.root, path)
        if target is None or not target.is_file():
            return default
        stat = target.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            cached = self.cache.get(target)
            if cached and cached[0] == stamp:
                self.cache.move_to_end(target)
                return cached[1]
        try:
            value = json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return default
        with self.lock:
            self.cache[target] = (stamp, value)
            self.cache.move_to_end(target)
            while len(self.cache) > 24:
                self.cache.popitem(last=False)
        return value

    def chips(self):
        batch = self.read(self.root / 'batch.json', {})
        states = batch.get('chip_states', {})
        # Completed manifests survive status-file interruptions and also support
        # standalone saved campaigns. Never recurse into archived attempts.
        names = set(states) | {p.parent.name for p in self.root.glob('*/manifest.json')}
        return [(name, states.get(name, {})) for name in sorted(names)
                if name not in ('.', '..') and '/' not in name and '\\' not in name
                and confined(self.root, name) is not None]

    def analysis(self, chip, values=None):
        folder = self.root / chip
        candidates = [folder / 'analysis-staged-features']
        configured = (values or {}).get('analysis_folder')
        if configured:
            candidates.extend([self.root / configured, folder / configured])
        candidates.extend(folder / name for name in ('analysis-feature-contrast', 'analysis-material-selected', 'analysis-component-repair', 'analysis'))
        for candidate in candidates:
            candidate = confined(self.root, candidate)
            if candidate is None:
                continue
            summary = self.read(candidate / 'analysis.json')
            if isinstance(summary, dict):
                return candidate, summary, self.read(candidate / 'analysis-progress.json', {})
        return None, {}, {}

    def index(self):
        fields, chips = [], []
        for chip, values in self.chips():
            folder = self.root / chip
            manifest = self.read(folder / 'manifest.json', {})
            analysis, summary, progress = self.analysis(chip, values)
            analysed = {t.get('tile_id') or str(t.get('key', '')).rsplit('/', 1)[-1]: t
                        for t in summary.get('tiles', [])}
            failures = analysis_failures(summary)
            supplements, supplement_errors = load_supplements(folder, analysis.name if analysis else None, failures, manifest)
            completed = manifest.get('completed', {})
            previews = sorted((folder / 'tiles').glob('*.jpg'))
            if manifest.get('coverage_kind'):
                previews = [p for p in previews if p.stem in completed]
            count = 0
            ready_count = failed_count = recovered_count = excluded_count = unresolved_count = 0
            for preview in previews:
                name = preview.stem
                tile = analysed.get(name, {})
                record = completed.get(name, {})
                ready = bool(tile) and (not tile.get('observations_file') or confined(analysis, tile['observations_file']) is not None and (analysis / tile['observations_file']).is_file())
                supplement = supplements.get(name) if not ready else None
                status = supplement['status'] if supplement else 'ready' if ready else 'failed' if name in failures else 'pending'
                error = failures.get(name) if status == 'failed' else None
                ready = ready or status == 'recovered'
                counts = {key: (supplement or tile).get(key) for key in ('proposal_count', 'selected_count', 'rejected_count')}
                fields.append({'key': chip + '/' + name, 'chip_id': chip, 'tile_id': name,
                    'preview': file_url(self.root, preview), 'preview_version': preview.stat().st_mtime_ns,
                    'position': record.get('position') or tile.get('position'),
                    'analysis_ready': ready, 'analysis_status': status,
                    'analysis_error': error, 'analysis_reason': supplement.get('reason') if supplement else None,
                    'analysis_version': supplement.get('version') if supplement else None,
                    'original_analysis_error': failures.get(name), **counts})
                count += 1
                ready_count += int(ready)
                failed_count += int(error is not None)
                recovered_count += int(status == 'recovered')
                excluded_count += int(status == 'excluded')
                unresolved_count += int(status == 'unresolved')
            chips.append({'id': chip, 'image_count': count, 'analysed_images': ready_count,
                'failed_images': failed_count, 'recovered_images': recovered_count,
                'excluded_images': excluded_count, 'unresolved_images': unresolved_count,
                'pending_images': count-ready_count-failed_count-excluded_count-unresolved_count,
                'supplement_errors': supplement_errors,
                'analysis_state': progress.get('state', 'ready' if analysed else 'pending'),
                'analysis_progress': progress, 'acquisition_state': values.get('state')})
        return {'chips': chips, 'fields': fields, 'image_count': len(fields),
            'qualification': 'Provisional optical features. Selection is a screening result, not material or layer identification.'}

    def field(self, chip, tile_id):
        states = dict(self.chips())
        if chip not in states or tile_id in ('.', '..') or any(c in tile_id for c in '/\\:'):
            raise KeyError('Unknown field')
        folder = self.root / chip
        preview = confined(self.root, folder / 'tiles' / (tile_id + '.jpg'))
        manifest = self.read(folder / 'manifest.json', {})
        completed = manifest.get('completed', {})
        if preview is None or not preview.is_file() or manifest.get('coverage_kind') and tile_id not in completed:
            raise KeyError('Unknown field')
        analysis, summary, progress = self.analysis(chip, states[chip])
        failures = analysis_failures(summary)
        supplements, supplement_errors = load_supplements(folder, analysis.name if analysis else None, failures, manifest)
        tile = next((t for t in summary.get('tiles', [])
                     if (t.get('tile_id') or str(t.get('key', '')).rsplit('/', 1)[-1]) == tile_id), {})
        rows, report, provenance = [], {}, {}
        ready = False
        supplement = supplements.get(tile_id) if not tile else None
        if supplement:
            shard = self.read(supplement['record_path'])
            # Registry validation checks both bytes and identities; any later
            # read failure leaves the original failure visible.
            if isinstance(shard, dict):
                tile = shard.get('tile', {})
                rows, report, provenance = shard.get('rows', []), shard.get('report', {}), shard.get('provenance', {})
                ready = supplement['status'] == 'recovered'
            else:
                supplement = None
        elif tile.get('observations_file'):
            shard_path = confined(analysis, tile['observations_file'])
            shard = self.read(shard_path) if shard_path is not None else None
            if isinstance(shard, dict):
                rows, report, provenance = shard.get('rows', []), shard.get('report', {}), shard.get('provenance', {})
                ready = True
        elif tile and analysis:
            aggregate = self.read(analysis / 'observations.json', {})
            observations = aggregate.get('observations', []) if isinstance(aggregate, dict) else aggregate
            key = tile.get('key', chip + '/' + tile_id)
            rows = [r for r in observations if r.get('tile_key') == key or r.get('source_tile') == tile_id]
            ready = isinstance(observations, list) and bool(aggregate)
        # Add a display decision without modifying the saved scientific rows.
        rows = [{**row, 'gallery_selected': bool(row.get('selected_for_review')) if 'selected_for_review' in row
                 else row.get('disposition') in ('candidate', 'selected') and not row.get('filter_reasons')} for row in rows]
        selected = sum(r['gallery_selected'] for r in rows)
        record = completed.get(tile_id, {})
        mask_path = confined(analysis, tile['instance_mask']) if analysis and tile.get('instance_mask') else None
        if supplement and supplement.get('instance_mask'):
            mask_path = supplement['supplement_folder']/supplement['instance_mask']
        status = supplement['status'] if supplement else 'ready' if ready else 'failed' if tile_id in failures else 'pending'
        error = failures.get(tile_id) if status == 'failed' else None
        recovery = {key:supplement.get(key) for key in ('supplement_id','version','fingerprint','source_identity','source_tiff_sha256','record_file','original_outcome','exclusion_kind')} if supplement else None
        return {'key': chip + '/' + tile_id, 'chip_id': chip, 'tile_id': tile_id,
            'preview': file_url(self.root, preview), 'shape': tile.get('shape') or manifest.get('camera_shape'),
            'pixel_size_um': tile.get('pixel_size_um') or manifest.get('transform', {}).get('pixel_size_um'),
            'position': record.get('position') or tile.get('position'),
            'focus_method': record.get('focus_method'), 'focus_limited': record.get('focus_limited'),
            'source_sha256': tile.get('sha256') or record.get('hashes', {}).get('tiles/' + tile_id + '.tiff'),
            'analysis_ready': ready, 'analysis_state': progress.get('state'),
            'analysis_status': status, 'analysis_error': error,
            'analysis_reason': supplement.get('reason') if supplement else None,
            'original_analysis_error': failures.get(tile_id), 'recovery': recovery, 'supplement_errors': supplement_errors,
            'analysis_name': supplement['supplement_folder'].name if supplement else analysis.name if analysis else None, 'rows': rows,
            'counts': {'proposal_count': len(rows), 'selected_count': selected, 'rejected_count': len(rows)-selected},
            'instance_mask': file_url(self.root, mask_path) if mask_path else None,
            'report': report, 'provenance': provenance}
