"""Loopback-only acquisition progress viewer; only mutation requests batch STOP."""
from pathlib import Path
import json
import mimetypes
import secrets
import threading
import time
from urllib.parse import quote, unquote, urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import atomic_json
from .gallery import FieldGallery, analysis_failures
from .gallery_supplement import load_supplements
from .contrast_plot import ContrastDistribution


def _inside(root, path):
    path = Path(path)
    result = (path if path.is_absolute() else root/path).resolve()
    return result if result.is_relative_to(root) else None


def activity_root(root):
    """Explicitly registered companion run, confined to this data directory."""
    # A campaign owns an in-root discovery directory; its latest saved attempt
    # supersedes the one-time registration of the first chip.
    try:
        batch=json.loads((root/'batch.json').read_text(encoding='utf-8'))
        if batch.get('coverage_kind'):
            candidates=[p for p in (root/'discovery').glob('*/discovery.json') if _inside(root,p) is not None]
            if candidates:
                latest=max(candidates,key=lambda p:p.stat().st_mtime_ns)
                return latest.parent,{'kind':'boundary_discovery','chip_id':latest.parent.name}
    except (OSError,ValueError,TypeError):pass
    path=root/'activity.json'
    if not path.is_file():return None,None
    try:
        registration=json.loads(path.read_text(encoding='utf-8'))
        if registration.get('kind')!='boundary_discovery':return None,None
        folder=_inside(root.parent,root/registration['folder'])
        if folder is None or folder==root.parent or not (folder/'discovery.json').is_file():return None,None
        return folder,registration
    except (OSError,ValueError,TypeError,KeyError):return None,None


def activity_state(root):
    folder,registration=activity_root(root)
    if folder is None:return None
    try:
        data=json.loads((folder/'discovery.json').read_text(encoding='utf-8'))
        records=data.get('records',[]);last=records[-1] if records else {}
        state={'kind':'boundary_discovery','title':'Measuring chip edges','chip_id':registration.get('chip_id'),
               'state':data.get('state'),'updated_at':data.get('updated_at'),'probe_count':len(records),
               'probe_budget':data.get('probe_budget'),'phase':last.get('phase','starting'),
               'position_um':last.get('position_um'),'ray_status':data.get('ray_status',{}),
               'enclosure':data.get('enclosure'),'error':data.get('error'),
               'stop_requested':(folder/'STOP').exists(),'thumbnail':None}
        if last.get('image'):
            preview=_inside(folder,Path(last['image']).with_suffix('.jpg'))
            if preview is not None and preview.is_file():
                state.update(thumbnail='/activity-files/'+quote(preview.relative_to(folder).as_posix(),safe='/'),
                             thumbnail_version=preview.stat().st_mtime_ns)
        return state
    except (OSError,ValueError,TypeError,KeyError):return {'state':'unavailable','title':'Boundary status temporarily unavailable'}


def calibration_state(root):
    """Saved focus evidence only; file timestamps never imply hardware activity."""
    folder=root/'calibration'
    records=[]
    for result in folder.glob('focus-*/focus-result.json'):
        try:
            value=json.loads(result.read_text(encoding='utf-8'))
            image=next((result.parent/name for name in ('focused.jpg','best-observed.jpg','best_observed.jpg') if (result.parent/name).is_file()),None)
            records.append({'name':result.parent.name,'success':value.get('success') is True,
                'reason':value.get('reason'),'z_um':value.get('z_um'),'samples':len(value.get('samples',[])),
                'saved_at':result.stat().st_mtime,'thumbnail':None if image is None else '/files/'+quote(image.relative_to(root).as_posix(),safe='/'),
                'thumbnail_version':None if image is None else image.stat().st_mtime_ns})
        except (OSError,ValueError,TypeError):continue
    if not records:return None
    records.sort(key=lambda r:r['saved_at'])
    return {'latest':records[-1],'completed_checks':len(records),'successful_checks':sum(r['success'] for r in records),
            'qualification':'Saved calibration evidence; not an indication of current microscope motion.'}


def batch_state(folder):
    """Read each polling snapshot afresh; acquisition owns all status fields."""
    root = Path(folder).resolve()
    path = root/'batch.json'
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'state': 'waiting', 'phase': 'Waiting for batch.json', 'chip_states': {}}
    state['stop_requested'] = (root/'STOP').exists()
    state['viewer_read_at'] = time.time()
    state['activity']=activity_state(root)
    state['calibration']=calibration_state(root)
    state['whole_chip_campaign']=bool(state.get('coverage_kind'))
    if state['whole_chip_campaign']:
        from .live_progress import build_live_progress
        try:
            state['live_progress'] = build_live_progress(root)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            state['live_progress'] = {'available': False, 'error': 'Live progress is temporarily unavailable: '+str(exc)}
    state['layout_image'] = '/files/layout.png' if (root/'layout.png').is_file() else None
    chips = []
    manifests = {}
    for chip_id in state.get('chip_states', {}):
        chip_root = _inside(root, chip_id)
        if chip_root is not None and (chip_root/'manifest.json').is_file():
            try: manifests[chip_id] = json.loads((chip_root/'manifest.json').read_text(encoding='utf-8'))
            except (OSError, ValueError): pass
    geometry = next((m for m in manifests.values() if m.get('camera_shape') and m.get('transform')), None)
    configuration = None
    if geometry is not None and (root/'configuration.json').is_file():
        try: configuration = json.loads((root/'configuration.json').read_text(encoding='utf-8'))
        except (OSError, ValueError): pass
    for chip_id, values in state.get('chip_states', {}).items():
        if not isinstance(values, dict):
            continue
        chip = {**values, 'id': chip_id, 'thumbnail': None, 'review_url': None}
        chip['gallery_url'] = '/gallery?chip='+quote(chip_id,safe='')
        chip_root = _inside(root, chip_id)
        if chip_root is None:
            continue
        manifest=manifests.get(chip_id,{})
        if manifest.get('coverage_kind'):
            chip['completed_tiles']=len(manifest.get('completed',{}))
            chip['planned_area_acquired']=manifest.get('planned_area_acquired',False)
            chip['focus_points_measured']=len(manifest.get('focus_points_um',[]))
            chip['focus_anchors_planned']=manifest.get('full_chip_options',{}).get('anchor_count')
            chip['coverage_kind']=manifest['coverage_kind']
        if chip.get('total_tiles') is None:
            manifest = manifests.get(chip_id,{})
            if manifest is not None and manifest.get('tile_count') is not None:
                chip['total_tiles'] = manifest['tile_count']
                chip['tile_count_source'] = 'acquisition manifest'
            elif configuration is not None and state.get('patch_size_um') and chip.get('centre_um'):
                # Plan geometry only: no camera connection, image allocation,
                # autofocus, or microscope action occurs in the viewer.
                from types import SimpleNamespace
                from .scan import ScanPlan, tile_centres
                from .model import CameraTransform
                try:
                    transform = CameraTransform(**configuration['camera_transform'])
                    if configuration['camera_transform'] != geometry['transform']:
                        raise ValueError('Acquired and configured camera geometry differ.')
                    x,y = chip['centre_um'];half = state['patch_size_um']/2
                    plan = ScanPlan([x-half,x+half,y-half,y+half], overlap=configuration.get('interior_scan',{}).get('overlap',.15))
                    shape = SimpleNamespace(rgb=SimpleNamespace(shape=geometry['camera_shape']), pixel_size_um=transform.pixel_size_um)
                    chip['total_tiles'] = len(tile_centres(plan, shape, transform))
                    chip['tile_count_source'] = 'planned from configured patch and acquired camera geometry'
                except (KeyError, ValueError, TypeError): pass
        tile_dir = chip_root/'tiles'
        chip['preview_images']=[]
        if tile_dir.is_dir():
            images = list(tile_dir.glob('*.jpg'))
            if manifest.get('coverage_kind'):
                completed=manifest.get('completed',{})
                images=[p for p in images if p.stem in completed]
            if images:
                chip['preview_images']=[{'url':'/files/'+quote(p.relative_to(root).as_posix(),safe='/'),'name':chip_id+' / '+p.stem} for p in sorted(images)]
                newest = max(images, key=lambda p: p.stat().st_mtime_ns)
                chip['thumbnail'] = '/files/'+quote(newest.relative_to(root).as_posix(), safe='/')
                chip['thumbnail_tile'] = newest.stem
                chip['thumbnail_version'] = newest.stat().st_mtime_ns
                chip['thumbnail_kind'] = 'survey_tile'
        if chip['thumbnail'] is None:
            focus_path = chip_root/'focus-surface'/'focus-surface.json'
            if focus_path.is_file():
                try:
                    focus = json.loads(focus_path.read_text(encoding='utf-8'))
                    verified = []
                    for anchor in focus.get('anchors', []):
                        if anchor.get('status') != 'verified' or not anchor.get('image'):
                            continue
                        candidate = _inside(root, focus_path.parent/Path(anchor['image']).with_suffix('.jpg'))
                        if candidate is not None and candidate.is_file():
                            verified.append((candidate, anchor.get('index')))
                    if verified:
                        newest,index = max(verified, key=lambda item:item[0].stat().st_mtime_ns)
                        chip['thumbnail'] = '/files/'+quote(newest.relative_to(root).as_posix(), safe='/')
                        chip['thumbnail_version'] = newest.stat().st_mtime_ns
                        chip['thumbnail_tile'] = 'anchor '+str(index+1) if isinstance(index,int) else 'verified anchor'
                        chip['thumbnail_kind'] = 'focus_anchor'
                except (OSError, ValueError, TypeError): pass
        if chip['thumbnail'] is None and state['whole_chip_campaign']:
            # A real pre-raster image is useful, but must never count as a tile.
            evidence=list((chip_root/'focus-surface').rglob('*.jpg'))
            if (chip_root/'overview.jpg').is_file():evidence.append(chip_root/'overview.jpg')
            if evidence:
                latest=max(evidence,key=lambda p:p.stat().st_mtime_ns)
                chip.update(thumbnail='/files/'+quote(latest.relative_to(root).as_posix(),safe='/'),
                    thumbnail_tile=latest.stem,thumbnail_version=latest.stat().st_mtime_ns,thumbnail_kind='focus_evidence')
        if chip['thumbnail'] is None and state['calibration']:
            latest=state['calibration']['latest']
            chip.update(thumbnail=latest['thumbnail'],thumbnail_tile=latest['name'],
                thumbnail_version=latest['thumbnail_version'],thumbnail_kind='calibration_reference')
        analysis = _inside(root, values.get('analysis_folder') or chip_root/'analysis')
        # Some producers record "analysis" relative to the chip directory.
        if analysis is not None and not (analysis/'review.html').is_file():
            relative = Path(values.get('analysis_folder') or 'analysis')
            if not relative.is_absolute():
                alternative = _inside(root, chip_root/relative)
                if alternative is not None and (alternative/'review.html').is_file():
                    analysis = alternative
        if analysis is not None and (analysis/'review.html').is_file():
            chip['review_url'] = '/files/'+quote((analysis/'review.html').relative_to(root).as_posix(), safe='/')
        chip['extra_reviews']=[]
        material_review=chip_root/'analysis-material-selected'/'review.html'
        if material_review.is_file():
            chip['review_url']='/files/'+quote(material_review.relative_to(root).as_posix(),safe='/')
            try:
                selected=json.loads((material_review.parent/'analysis.json').read_text(encoding='utf-8')).get('material_selection',{})
                chip['material_selection']=selected
            except (OSError,ValueError):pass
        thin_review=chip_root/'analysis-feature-contrast'/'review.html'
        if thin_review.is_file():
            if material_review.is_file():
                chip['extra_reviews'].append({'label':'Inspect bulk/cyan reference colors','url':'/files/'+quote(material_review.relative_to(root).as_posix(),safe='/')})
            chip['review_url']='/files/'+quote(thin_review.relative_to(root).as_posix(),safe='/')
            chip['review_kind']='feature_contrast'
            try:
                thin=json.loads((thin_review.parent/'analysis.json').read_text(encoding='utf-8'))
                chip['material_selection']=thin.get('material_selection',{})
                chip['thin_analysis_images']=len(thin.get('tiles',[]))
                chip['thin_analysis_failed_images']=len(thin.get('failed_tiles',[]))
            except (OSError,ValueError):pass
        for directory,label in [('analysis-component-repair','Inspect updated object boundaries'),('analysis-bulk-reference','Inspect bulk-color masks · all sizes'),('analysis-v2','Inspect revised faint-region proposals')]:
            extra=chip_root/directory/'review.html'
            if extra.is_file():chip['extra_reviews'].append({'label':label,'url':'/files/'+quote(extra.relative_to(root).as_posix(),safe='/')})
        staged=chip_root/'analysis-staged-features'
        if (staged/'analysis-progress.json').is_file():
            try:
                chip['staged_analysis']=json.loads((staged/'analysis-progress.json').read_text(encoding='utf-8'))
                if (staged/'analysis.json').is_file():
                    summary=json.loads((staged/'analysis.json').read_text(encoding='utf-8'))
                    progress=chip['staged_analysis']
                    failures=analysis_failures(summary)
                    supplement,supplement_errors=load_supplements(chip_root,staged.name,failures,manifest)
                    progress['original_failed_tiles']=len(failures)
                    progress['failed_tiles']=len(failures)-len(supplement)
                    for status in ('recovered','excluded','unresolved'):
                        progress[status+'_tiles']=sum(entry['status']==status for entry in supplement.values())
                    progress['primary_completed_tiles']=progress.get('completed_tiles',0)
                    progress['completed_tiles']=progress['primary_completed_tiles']+progress['recovered_tiles']
                    progress['pending_tiles']=max(0,progress.get('total_tiles',0)-progress['completed_tiles']-progress['failed_tiles']-progress['excluded_tiles']-progress['unresolved_tiles'])
                    progress['supplement_errors']=supplement_errors
            except (OSError,ValueError):pass
        chips.append(chip)
    state['chips'] = chips
    return state


def _read_only_review(html):
    # Keep existing source/candidate browsing while disabling all annotation
    # edits and server-save setup. review.py itself remains unchanged.
    html = html.replace("if(location.protocol==='http:')Promise.all", 'if(false)Promise.all')
    injection = """<script>
if(typeof mark==='function')mark=()=>{};
if(typeof save==='function')save=async()=>{};
for(const id of ['drawMode','finishPolygon','clearDraft','newRoi','saveRoi','deleteRoi','roiLabel','roiNote','note']){const el=document.getElementById(id);if(el)el.disabled=true;}
document.querySelectorAll('[data-decision]').forEach(el=>el.disabled=true);
const draw=document.getElementById('drawMode');if(draw)draw.value='view';
const status=document.getElementById('saveState');if(status)status.textContent='Read-only batch gallery';
const banner=document.createElement('div');banner.style.cssText='padding:10px 20px;background:#15372d;color:#d3ffed;border-bottom:1px solid #5a9a80';
const back=document.createElement('a');back.href='/';back.textContent='← Live batch';banner.append(back,document.createTextNode(' · Read-only image review. Proposals remain unvalidated; material and layer count are unconfirmed.'));document.body.prepend(banner);
</script>"""
    return html.replace('</body>', injection+'</body>')


def serve_batch(folder, port=0, open_browser=False):
    """Return (server,url). Caller owns lifetime; binding is always 127.0.0.1."""
    root = Path(folder).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    template = Path(__file__).parent/'batch.html'
    gallery = FieldGallery(root)
    distribution = ContrastDistribution(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, kind='application/json', cache='no-store'):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, b'{}')
            path = unquote(urlsplit(self.path).path)
            query = parse_qs(urlsplit(self.path).query)
            if path in ('/', '/batch.html'):
                return self.reply(200, template.read_bytes(), 'text/html; charset=utf-8')
            if path in ('/gallery', '/gallery.html'):
                return self.reply(200, template.with_name('gallery.html').read_bytes(), 'text/html; charset=utf-8')
            if path in ('/contrast', '/contrast.html'):
                return self.reply(200, template.with_name('contrast.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/contrast-distribution':
                try:
                    return self.reply(200, distribution.data(query.get('since',[None])[0]))
                except (OSError,ValueError,TypeError):
                    return self.reply(503,b'{"error":"Saved object measurements are temporarily unavailable."}')
            if path in ('/api/gallery', '/api/gallery-field'):
                try:
                    payload = gallery.index() if path == '/api/gallery' else gallery.field(query['chip'][0], query['tile'][0])
                    return self.reply(200, json.dumps(payload,allow_nan=False).encode())
                except KeyError:
                    return self.reply(404,b'{"error":"Unknown saved field."}')
                except (OSError,ValueError,TypeError):
                    return self.reply(503,b'{"error":"Saved analysis is temporarily unavailable."}')
            if path == '/api/session':
                return self.reply(200, json.dumps({'token': token}).encode())
            if path == '/api/state':
                try:
                    body = json.dumps(batch_state(root), allow_nan=False).encode()
                    return self.reply(200, body)
                except (OSError, ValueError, TypeError):
                    return self.reply(503, b'{"error":"Status file is unavailable; acquisition may still be running."}')
            if path.startswith('/activity-files/'):
                folder,_=activity_root(root)
                target=None if folder is None else _inside(folder,path[len('/activity-files/'):])
                if target is None or not target.is_file() or target.suffix.lower() not in ('.jpg','.jpeg','.png'):
                    return self.reply(404,b'{}')
                return self.reply(200,target.read_bytes(),mimetypes.guess_type(str(target))[0] or 'application/octet-stream')
            if not path.startswith('/files/'):
                return self.reply(404, b'{}')
            target = _inside(root, path[len('/files/'):])
            if target is None or not target.is_file() or target.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.html'):
                return self.reply(404, b'{}')
            if target.suffix.lower() == '.html':
                if target == root/'thin-shortlist.html':
                    return self.reply(200, target.read_bytes(), 'text/html; charset=utf-8')
                if target.name != 'review.html':
                    return self.reply(404, b'{}')
                return self.reply(200, _read_only_review(target.read_text(encoding='utf-8')).encode(), 'text/html; charset=utf-8')
            cache='private, max-age=3600' if query.get('v')==[str(target.stat().st_mtime_ns)] else 'no-store'
            return self.reply(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or 'application/octet-stream',cache)

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if not self.valid_host() or self.headers.get('Origin') != origin or self.headers.get('X-Batch-Token') != token:
                return self.reply(403, b'{}')
            if urlsplit(self.path).path != '/api/stop':
                return self.reply(404, b'{}')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 512:
                    raise ValueError('Request size')
                body = json.loads(self.rfile.read(size))
                if body != {}:
                    raise ValueError('Unexpected request body')
                with lock:
                    operation=activity_state(root)
                    folder,_=activity_root(root)
                    campaign=False
                    if (root/'batch.json').is_file():
                        campaign=bool(json.loads((root/'batch.json').read_text(encoding='utf-8')).get('coverage_kind'))
                    stop_root=folder if not campaign and operation and operation.get('state')=='running' and folder is not None else root
                    if not (stop_root/'STOP').exists():
                        atomic_json(stop_root/'STOP', {'requested_at': time.time(), 'source': 'local batch viewer'})
                return self.reply(200, b'{"stop_requested":true}')
            except (ValueError, TypeError):
                return self.reply(400, b'{}')

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/'
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return server, url


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('folder')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server, url = serve_batch(args.folder, args.port)
    print(url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
