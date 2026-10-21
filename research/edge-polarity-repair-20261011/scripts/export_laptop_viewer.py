"""Freeze gallery APIs and measured object contrast for a stdlib-only laptop viewer.

No images, TIFFs, analysis files or acquisition settings are modified or copied.
"""
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit
import argparse
import gzip
import hashlib
import html
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flakefinder.gallery import FieldGallery
from flakefinder.contrast_plot import CONVENTION, number, profile, vector


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_bytes(data)
    temp.replace(path)


def portable_paths(value, root):
    """Presentation-only paths; exact original metadata remains in the dataset."""
    if isinstance(value, dict):
        return {k:portable_paths(v, root) for k,v in value.items()}
    if isinstance(value, list):
        return [portable_paths(v, root) for v in value]
    if isinstance(value, str):
        text = value.replace('\\', '/')
        prefix = root.as_posix().rstrip('/')+'/'
        if text.lower().startswith(prefix.lower()):
            return text[len(prefix):]
    return value


def add_points(data, context, distribution, profiles, groups):
    if data['analysis_status'] not in ('ready', 'recovered'):
        return
    identifier, settings = profile(data.get('provenance', {}).get('context') or context)
    profiles[identifier] = settings
    field_index = len(distribution['fields'])
    distribution['fields'].append({'key':data['key'],'chip':data['chip_id'],'tile':data['tile_id'],
        'status':data['analysis_status'],'version':(data.get('recovery') or {}).get('version') or data['analysis_name'],
        'preview':data['preview'],'shape':data['shape']})
    group = groups.setdefault((data['chip_id'],data['analysis_status'],identifier),
        {'chip':data['chip_id'],'status':data['analysis_status'],'profile':identifier,
         'counts':Counter(),'unmeasured_reasons':Counter()})
    counts = group['counts'];position = data.get('position') or {}
    for row in data['rows']:
        counts['total'] += 1
        selected = row['gallery_selected']
        counts['selected' if selected else 'rejected'] += 1
        measurement = row.get('local_contrast_measurement') or {}
        contrast = vector(measurement.get('contrast_rgb'), 3)
        if measurement.get('measurement_status') != 'measured' or contrast is None:
            counts['unmeasured'] += 1
            group['unmeasured_reasons'][measurement.get('reason') or 'No finite local RGB measurement'] += 1
            continue
        counts['measured'] += 1
        area, width = number(row.get('area_um2')), number(row.get('usable_width_um'))
        useful = bool(selected and area is not None and area >= 100 and width is not None and width >= 5)
        counts['useful_measured'] += int(useful)
        xy = vector(row.get('stage_xy_um'), 2) or [number(position.get('x')), number(position.get('y'))]
        distribution['points'].append({'id':str(row.get('id') or 'instance-'+str(row.get('instance_label'))),
            'c':contrast,'a':area,'w':width,'s':selected,'u':useful,'xyz':[*xy,number(position.get('z'))],
            'b':vector(row.get('bbox_px'),4),'se':vector(measurement.get('standard_error_rgb'),3),
            'f':field_index,'p':identifier})


def export(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not (output/'Launcher.py').is_file():
        raise ValueError('Output must contain the portable Launcher.py template')
    started = time.time();gallery = FieldGallery(root);index = gallery.index()
    artifacts, fields, assets, profiles, groups = {}, {}, {}, {}, {}
    statuses = Counter()
    distribution = {'schema':1,'built_at':started,'convention':CONVENTION,
        'measurement_field':'local_contrast_measurement.contrast_rgb',
        'qualification':'One point per measured source-object observation. Overlapping fields can repeat the same physical object. No material or layer identity is assigned.',
        'fields':[],'points':[],'warnings':[]}

    def publish(name, value):
        raw = encoded(value)
        body = gzip.compress(raw, compresslevel=3, mtime=0) if name.endswith('.gz') else raw
        write(output/name, body)
        artifacts[name] = hashlib.sha256(body).hexdigest()

    def register(url):
        if not url:
            return
        relative = unquote(urlsplit(url).path).removeprefix('/files/')
        path = (root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in ('.jpg','.jpeg','.png'):
            raise ValueError('Missing or escaped viewer image: '+relative)
        assets[relative] = {'bytes':path.stat().st_size}

    contexts = {chip:gallery.read(root/chip/'manifest.json', {}).get('context', {}) for chip,_ in gallery.chips()}
    for n,item in enumerate(index['fields']):
        data = portable_paths(gallery.field(item['chip_id'],item['tile_id']), root)
        filename = 'snapshots/fields/'+hashlib.sha256(data['key'].encode()).hexdigest()[:24]+'.json.gz'
        publish(filename,data);fields[data['key']] = filename
        for key in ('preview','instance_mask'):
            register(data.get(key))
        for key in ('analysis_ready','analysis_status','analysis_error','analysis_reason','original_analysis_error'):
            item[key] = data[key]
        item.update(data['counts'])
        statuses[data['analysis_status']] += 1
        add_points(data, contexts[data['chip_id']], distribution, profiles, groups)
        if (n+1)%100 == 0 or n+1 == len(index['fields']):
            print('Exported '+str(n+1)+' / '+str(len(index['fields']))+' fields', flush=True)

    for chip in index['chips']:
        entries = [f for f in index['fields'] if f['chip_id']==chip['id']]
        chip['original_analysis_state'] = chip['analysis_state'];chip['analysis_state'] = 'saved snapshot'
        for key,status in (('failed_images','failed'),('recovered_images','recovered'),('excluded_images','excluded'),('unresolved_images','unresolved'),('pending_images','pending')):
            chip[key] = sum(f['analysis_status']==status for f in entries)
        chip['analysed_images'] = sum(f['analysis_ready'] for f in entries)
    distribution['groups'] = list(groups.values())
    distribution['profiles'] = [{'id':key,'settings':value} for key,value in sorted(profiles.items())]
    counts = Counter()
    for group in groups.values():
        counts.update(group['counts'])
    distribution['counts'] = dict(counts);distribution['field_statuses'] = dict(statuses)
    distribution['signature'] = hashlib.sha256(encoded(distribution)).hexdigest()
    publish('snapshots/contrast.json.gz', distribution);publish('snapshots/gallery.json.gz', index)
    for name in ('gallery.html','contrast.html'):
        page = (ROOT/'flakefinder'/name).read_text(encoding='utf-8')
        page = page.replace('← Live acquisition','← Saved campaign').replace('>Live scan</a>','>Saved campaign</a>')
        page = page.replace('poll();setInterval(poll,7000);','poll();')
        page = page.replace("'Updated '+new Date().toLocaleTimeString()", "'Offline saved snapshot'")
        page = page.replace('Its raw preview is available.', 'Its raw preview is available in this saved snapshot.')
        write(output/name,page.encode('utf-8'));artifacts[name] = hashlib.sha256(page.encode('utf-8')).hexdigest()
    finished = time.time()
    manifest = {'schema':1,'kind':'portable_microscopy_viewer','created_at':finished,'export_started_at':started,
        'data_relative':Path(os.path.relpath(root,output)).as_posix(),'dataset_name':root.name,
        'image_count':len(fields),'counts':dict(counts),'field_statuses':dict(statuses),
        'field_files':fields,'data_assets':assets,'artifacts':artifacts,'contrast_signature':distribution['signature'],
        'qualification':'Read-only saved snapshot. No microscope connection or segmentation. Raw frames, original analysis and provenance remain in the accompanying dataset. Display paths are dataset-root relative.',
        'chip_counts':index['chips']}
    cards = ''.join('<article><h2>'+html.escape(c['id'])+'</h2><p>'+str(c['image_count'])+' images · '+str(c['analysed_images'])+' analysed</p><p>'+str(c['recovered_images'])+' recovered · '+str(c['excluded_images'])+' excluded · '+str(c['unresolved_images']+c['failed_images'])+' unresolved/failed · '+str(c['pending_images'])+' pending at export</p><a href="/gallery?chip='+html.escape(c['id'],quote=True)+'">Browse images and outlines →</a></article>' for c in index['chips'])
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Saved microscopy campaign</title><style>body{font:16px system-ui;background:#0b131d;color:#e7eff5;max-width:1050px;margin:35px auto;padding:0 20px;line-height:1.5}h1{font-size:30px}a{color:#78e2b4}nav{display:flex;gap:25px;margin:24px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}article{background:#14222f;border:1px solid #314658;border-radius:8px;padding:20px}h2{margin-top:0}.muted{color:#a3b6c5}.notice{border-left:3px solid #e9bd7d;padding-left:15px}</style><h1>Saved microscopy campaign</h1><p class="muted">Offline, read-only snapshot · '''+html.escape(time.strftime('%Y-%m-%d %H:%M:%S %Z',time.localtime(finished)))+'''</p><p>'''+str(len(fields))+''' acquired images · '''+str(counts['measured'])+''' measured source features · '''+str(counts['useful_measured'])+''' measured useful-review features.</p><nav><a href="/gallery">All images and feature outlines</a><a href="/contrast">Interactive color-contrast distribution</a></nav><p class="notice">These are optical feature proposals, not confirmed MoTe₂ or assigned layer counts. Rejected features, recovery history and unmeasured objects remain reviewable. Overlapping fields may show the same physical feature more than once.</p><div class="cards">'''+cards+'''</div><p class="muted">This page reads frozen JSON and accompanying image previews. There are no camera, stage, microscope, acquisition or write controls. The original TIFFs and full analysis remain in the accompanying data folder. Pending or unresolved status is the recorded state at export.</p></html>'''
    write(output/'index.html',page.encode('utf-8'));artifacts['index.html'] = hashlib.sha256(page.encode('utf-8')).hexdigest()
    # Publish the manifest last, so an interrupted export is never advertised as complete.
    write(output/'snapshot.json',encoded(manifest))
    return {key:manifest[key] for key in ('image_count','counts','field_statuses','data_relative','created_at')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=ROOT/'data/whole-chip-standardized-01')
    parser.add_argument('--output',type=Path,default=ROOT/'laptop-viewer')
    args = parser.parse_args()
    print(json.dumps(export(args.data,args.output),indent=2))
