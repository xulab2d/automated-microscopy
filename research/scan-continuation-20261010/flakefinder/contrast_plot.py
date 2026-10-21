"""Compact cached distributions of saved object measurements; no image analysis."""
from collections import Counter
from pathlib import Path
from threading import RLock
import hashlib
import json
import math
import time

from .gallery import FieldGallery, analysis_failures, confined, file_url
from .gallery_supplement import load_supplements

PROFILE_KEYS = ('camera', 'objective', 'exposure_ms', 'illumination', 'illumination_percent',
                'white_balance', 'white_level', 'color_profile_kind', 'substrate', 'oxide_nm')
CONVENTION = '(local substrate - feature) / local substrate'


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def vector(value, length):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        return None
    converted = [number(x) for x in value]
    return converted if all(x is not None for x in converted) else None


def profile(context):
    settings = {key: context.get(key) for key in PROFILE_KEYS}
    encoded = json.dumps(settings, sort_keys=True, separators=(',', ':'))
    identifier = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    return identifier, settings


class ContrastDistribution:
    """Keep only compact extracted rows. Unchanged shards are never re-parsed."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.gallery = FieldGallery(self.root)
        self.lock = RLock()
        self.fields = {}
        self.signature = None
        self.encoded = None
        self.snapshot = None
        self.shard_reads = 0

    def sources(self):
        sources, warnings, field_statuses = [], [], Counter()
        for chip, values in self.gallery.chips():
            folder = self.root/chip
            manifest = self.gallery.read(folder/'manifest.json', {})
            analysis, summary, _ = self.gallery.analysis(chip, values)
            if analysis is None:
                continue
            supplements, errors = load_supplements(folder, analysis.name, analysis_failures(summary), manifest)
            warnings.extend(errors)
            failures = analysis_failures(summary)
            known = set()
            for tile in summary.get('tiles', []):
                tile_id = tile.get('tile_id') or str(tile.get('key', '')).rsplit('/', 1)[-1]
                key = chip+'/'+tile_id
                shard = confined(analysis, tile['observations_file']) if tile.get('observations_file') else None
                if shard is None or not shard.is_file():
                    warnings.append(key+': source observation checkpoint unavailable')
                    continue
                known.add(tile_id)
                sources.append(self._source(chip, tile_id, shard, 'ready', analysis.name, manifest, tile))
                field_statuses['ready'] += 1
            for tile_id, item in supplements.items():
                if tile_id in known:
                    continue
                field_statuses[item['status']] += 1
                if item['status'] == 'recovered':
                    sources.append(self._source(chip, tile_id, item['record_path'], 'recovered', item['version'], manifest, {}))
            field_statuses['failed'] += len(set(failures)-set(supplements))
        return sources, warnings, dict(field_statuses)

    def _source(self, chip, tile_id, path, status, version, manifest, tile):
        path = confined(self.root, path)
        if path is None:
            raise ValueError('Observation checkpoint escapes dataset')
        record = manifest.get('completed', {}).get(tile_id, {})
        return {'chip': chip, 'tile': tile_id, 'key': chip+'/'+tile_id, 'path': path,
            'status': status, 'version': version, 'context': manifest.get('context', {}),
            'position': tile.get('position') or record.get('position', {}),
            'shape': tile.get('shape') or manifest.get('camera_shape'),
            'preview': file_url(self.root, self.root/chip/'tiles'/(tile_id+'.jpg'))}

    def _extract(self, source):
        path = source['path'];stat = path.stat()
        context_signature = json.dumps(source['context'], sort_keys=True)
        signature = (stat.st_mtime_ns, stat.st_size, source['status'], source['version'], context_signature)
        cached = self.fields.get(path)
        if cached and cached[0] == signature:
            return cached[1], signature
        record = json.loads(path.read_text(encoding='utf-8'))
        self.shard_reads += 1
        context = record.get('provenance', {}).get('context') or source['context']
        profile_id, settings = profile(context)
        tile = record.get('tile', {})
        position = tile.get('position') or source['position']
        z = number(position.get('z'))
        rows, counts, reasons = [], Counter(), Counter()
        for row in record.get('rows', []):
            counts['total'] += 1
            selected = bool(row.get('selected_for_review'))
            counts['selected' if selected else 'rejected'] += 1
            measurement = row.get('local_contrast_measurement') or {}
            contrast = vector(measurement.get('contrast_rgb'), 3)
            if measurement.get('measurement_status') != 'measured' or contrast is None:
                counts['unmeasured'] += 1
                reasons[measurement.get('reason') or 'No finite local RGB measurement'] += 1
                continue
            counts['measured'] += 1
            area, width = number(row.get('area_um2')), number(row.get('usable_width_um'))
            useful = bool(selected and area is not None and area >= 100 and width is not None and width >= 5)
            counts['useful_measured'] += int(useful)
            xy = vector(row.get('stage_xy_um'), 2) or [number(position.get('x')), number(position.get('y'))]
            rows.append({'id': str(row.get('id') or 'instance-'+str(row.get('instance_label'))),
                'c': contrast, 'a': area, 'w': width, 's': selected, 'u': useful,
                'xyz': [*xy, z], 'b': vector(row.get('bbox_px'), 4),
                'se': vector(measurement.get('standard_error_rgb'), 3)})
        compact = {'profile_id': profile_id, 'settings': settings, 'points': rows,
                   'counts': dict(counts), 'unmeasured_reasons': dict(reasons),
                   'shape': tile.get('shape') or source['shape']}
        self.fields[path] = (signature, compact)
        return compact, signature

    def data(self, since=None):
        with self.lock:
            sources, warnings, statuses = self.sources()
            signature_parts, chunks = [], []
            active = set()
            for source in sources:
                try:
                    compact, signature = self._extract(source)
                except (OSError, ValueError, TypeError) as error:
                    warnings.append(source['key']+': '+str(error))
                    continue
                active.add(source['path'])
                signature_parts.append((source['key'], str(source['path'].relative_to(self.root)), signature))
                chunks.append((source, compact))
            self.fields = {key:value for key,value in self.fields.items() if key in active}
            signature = hashlib.sha256(json.dumps([signature_parts,warnings,statuses],sort_keys=True).encode()).hexdigest()
            if self.signature == signature:
                if since == signature:
                    return json.dumps({'unchanged':True,'signature':signature}).encode()
                return self.encoded
            profiles = {};groups = {};fields=[];points=[]
            for source, compact in chunks:
                profile_id = compact['profile_id']
                profiles[profile_id] = compact['settings']
                index = len(fields)
                fields.append({key:source[key] for key in ('key','chip','tile','status','version','preview')})
                fields[-1]['shape'] = compact['shape']
                group_key = (source['chip'], source['status'], profile_id)
                group = groups.setdefault(group_key, {'chip':source['chip'],'status':source['status'],
                    'profile':profile_id,'counts':Counter(),'unmeasured_reasons':Counter()})
                group['counts'].update(compact['counts'])
                group['unmeasured_reasons'].update(compact['unmeasured_reasons'])
                points.extend({**point,'f':index,'p':profile_id} for point in compact['points'])
            totals = Counter()
            for group in groups.values():
                totals.update(group['counts'])
            payload = {'schema':1,'signature':signature,'built_at':time.time(),
                'convention':CONVENTION,'measurement_field':'local_contrast_measurement.contrast_rgb',
                'qualification':'One point per measured source-object observation. Overlapping fields can repeat the same physical object. No material or layer identity is assigned.',
                'fields':fields,'points':points,'groups':list(groups.values()),'counts':dict(totals),
                'profiles':[{'id':identifier,'settings':settings} for identifier,settings in sorted(profiles.items())],
                'field_statuses':statuses,'warnings':warnings}
            self.encoded = json.dumps(payload,allow_nan=False,separators=(',',':')).encode()
            self.signature, self.snapshot = signature, payload
            return self.encoded
