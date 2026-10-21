"""Manifest-indexed project library, with lazy, hash-checked analysis reads.

No instrument imports. Paths in public records are project-relative and portable.
The image index reads manifests, not image pixels or thousands of file stats.
"""
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path, PurePosixPath
from threading import RLock
import json
import re


class ProjectCatalog:
    def __init__(self, project_root, campaign_rel="data/whole-chip-standardized-01"):
        self.root = Path(project_root).resolve(strict=True)
        self.campaign_rel = PurePosixPath(str(campaign_rel).replace('\\', '/')).as_posix()
        self.campaign = self.resolve(self.campaign_rel)
        marker = self.read('.flakefinder-project.json', {})
        self.project_id = marker.get('project_id') or marker.get('id')
        if not self.project_id:
            raise ValueError('Missing project identity marker; refusing an unrelated directory')
        self.dataset_id = re.sub(r'[^a-zA-Z0-9._-]', '-', self.campaign.name) + '-' + sha256(
            (self.project_id + '/' + self.campaign_rel).encode()).hexdigest()[:12]
        self._cache = OrderedDict()
        self._lock = RLock()

    def resolve(self, relative):
        rel = PurePosixPath(str(relative).replace('\\', '/'))
        if rel.is_absolute() or '..' in rel.parts or ':' in str(rel):
            raise ValueError('Expected a project-relative path')
        target = (self.root / Path(*rel.parts)).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError('Path escapes project root')
        return target

    def read(self, relative, default=None, expected_sha256=None):
        path = self.resolve(relative)
        try:
            st = path.stat()
        except FileNotFoundError:
            return default
        stamp = (st.st_mtime_ns, st.st_size, expected_sha256)
        lock = getattr(self, '_lock', RLock())
        with lock:
            cache = getattr(self, '_cache', {})
            old = cache.get(path)
            if old and old[0] == stamp:
                return old[1]
        raw = path.read_bytes()
        if expected_sha256 and sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('Analysis content hash mismatch: ' + str(relative))
        data = json.loads(raw.decode('utf-8-sig'))
        with lock:
            if hasattr(self, '_cache'):
                self._cache[path] = (stamp, data)
                self._cache.move_to_end(path)
                while len(self._cache) > 32:
                    self._cache.popitem(last=False)
        return data

    def _manifest_paths(self):
        return sorted(self.campaign.glob('*/manifest.json'))

    def catalog(self):
        fields, chips = [], []
        ledger = self.read(self.campaign_rel + '/analysis-residue-sieves-campaign-01/ledger.json', {})
        for path in self._manifest_paths():
            chip = path.parent.name
            manifest = self.read(path.relative_to(self.root).as_posix(), {})
            completed = manifest.get('completed', {})
            for tile, record in sorted(completed.items()):
                if not re.fullmatch(r'[A-Za-z0-9_.-]+', tile):
                    continue
                key = chip + '/' + tile
                base = self.campaign_rel + '/' + chip + '/tiles/' + tile
                shape = manifest.get('camera_shape', [])[:2]
                source_hash = record.get('hashes', {}).get('tiles/' + tile + '.tiff')
                identity = dict(dataset_id=self.dataset_id, field_key=key,
                    source_sha256=source_hash, shape_hw=shape, group_id=self.dataset_id + '/' + chip)
                item = dict(key=key, field_key=key, chip_id=chip, tile_id=tile,
                    preview_rel=base + '.jpg', raw_rel=base + '.tiff', metadata_rel=base + '.json',
                    source_identity=identity, source_sha256=source_hash, shape_hw=shape,
                    position_um=record.get('position'), position=record.get('position'),
                    pixel_size_um=manifest.get('transform', {}).get('pixel_size_um'),
                    focus_method=record.get('focus_method'), focus_limited=record.get('focus_limited'),
                    focus_evidence=record.get('full_chip_focus', {}),
                    analysis_status='unassessable' if ledger.get(key, {}).get('unassessable') else 'excluded' if ledger.get(key, {}).get('excluded') else 'sieved' if key in ledger else 'not_sieved',
                    analysis_exclusion_reason=ledger.get(key, {}).get('reason'),
                    analysis_counts=ledger.get(key, {}).get('summary', {}).get('counts', {}))
                fields.append(item)
            chips.append(dict(id=chip, image_count=len(completed), state=manifest.get('state'),
                raw_coverage_complete=manifest.get('raw_coverage_complete'),
                focus_coverage_complete=manifest.get('focus_coverage_complete'), context=manifest.get('context')))
        return dict(schema_version=1, project_id=self.project_id, dataset_id=self.dataset_id,
                    campaign_rel=self.campaign_rel, image_count=len(fields), chips=chips, fields=fields,
                    qualification='Optical screening; material and layer labels require calibration or independent measurement.')

    def get_field(self, field_key):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', field_key):
            raise KeyError('Invalid field key')
        item = next((r for r in self.catalog()['fields'] if r['key'] == field_key), None)
        if item is None:
            raise KeyError('Unknown field')
        chip, tile = field_key.split('/')
        analysis_rel = self.campaign_rel + '/analysis-residue-sieves-campaign-01'
        entry = self.read(analysis_rel + '/ledger.json', {}).get(field_key)
        if entry and not entry.get('excluded'):
            # Old workers wrote absolute Windows paths. Rebase only the known
            # final attempt name inside this campaign, never an arbitrary suffix.
            name = str(entry.get('folder', '')).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
            if not re.fullmatch(re.escape(field_key.replace('/', '__')) + r'-[a-f0-9]+', name):
                raise ValueError('Unexpected analysis attempt path')
            rel = analysis_rel + '/field-attempts/' + name + '/fields/' + field_key.replace('/', '__') + '.json'
            expected = next((h for p, h in entry.get('hashes', {}).items()
                             if str(p).replace('\\', '/').endswith('/fields/' + field_key.replace('/', '__') + '.json')), None)
            if not expected:
                raise ValueError('Completed analysis is missing its content hash')
            shard = self.read(rel, expected_sha256=expected)
            if shard is None:
                raise FileNotFoundError('Analysis not available on this computer yet: ' + rel)
            for row in shard.get('rows', []):
                if row.get('source_tiff_sha256') != item['source_sha256']:
                    raise ValueError('Analysis source differs from acquisition manifest')
            item.update(rows=shard.get('rows', []), report=shard.get('report', {}),
                        analysis_name='residue-sieves-campaign-01', analysis_record_rel=rel)
        elif entry and entry.get('excluded'):
            item.update(rows=[], report=entry.get('summary', {}), analysis_name='residue-sieves-campaign-01',
                        analysis_status='unassessable' if entry.get('unassessable') else 'excluded',
                        analysis_exclusion_reason=entry.get('reason'),
                        assessment_completed=False, source_features_retained=True)
        elif (reference := self.read(self.campaign_rel + '/analysis-residue-sieves-02/fields/' + field_key.replace('/', '__') + '.json')) is not None:
            for row in reference.get('rows', []):
                if row.get('source_tiff_sha256') != item['source_sha256']:
                    raise ValueError('Reference analysis source differs from acquisition manifest')
            item.update(rows=reference.get('rows', []),report=reference.get('report', {}),
                analysis_name='residue-sieves-02',analysis_status='reference_sieved',
                analysis_record_rel=self.campaign_rel + '/analysis-residue-sieves-02/fields/' + field_key.replace('/', '__') + '.json')
        else:
            # Existing reader handles recovery registries and preserves exclusions.
            from .gallery import FieldGallery
            old = FieldGallery(self.campaign).field(chip, tile)
            item.update({k: old[k] for k in ('rows', 'report', 'provenance', 'analysis_name', 'analysis_status', 'analysis_error') if k in old})
        item['metadata'] = self.read(item['metadata_rel'], {})
        return item

    def status(self):
        return {name: self.read(self.campaign_rel + '/' + rel, {}) for name, rel in {
            'analysis': 'analysis-residue-sieves-campaign-01/status.json',
            'acquisition': 'overnight.json', 'batch': 'batch.json'}.items()}
