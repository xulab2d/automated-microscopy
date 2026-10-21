"""Portable local review gallery; images are cropped from recorded raw tile identities."""
from pathlib import Path
import hashlib, json, math, re, shutil
import cv2
import numpy as np
from PIL import Image
from .imaging import load_frame, preview
from .model import atomic_json

def _clip_polygon(points,w,h):
    polygon=[list(map(float,p)) for p in points]
    for axis,edge,lower in ((0,0,True),(0,w-1,False),(1,0,True),(1,h-1,False)):
        result=[]
        if not polygon:break
        previous=polygon[-1];previous_in=previous[axis]>=edge if lower else previous[axis]<=edge
        for current in polygon:
            current_in=current[axis]>=edge if lower else current[axis]<=edge
            if current_in!=previous_in:
                t=(edge-previous[axis])/(current[axis]-previous[axis])
                result.append([previous[i]+t*(current[i]-previous[i]) for i in (0,1)])
            if current_in:result.append(current)
            previous,previous_in=current,current_in
        polygon=result
    return polygon if len(polygon)>=3 else []

def catalog_projections(rows,tiles):
    """Legacy fallback only: explicitly marked projections, never detections."""
    result=[]
    for tile in tiles:
        footprint=tile.get('footprint_um');h,w=tile['shape']
        origin=matrix=None
        if footprint and len(footprint)==4 and min(h,w)>1:
            origin=np.asarray(footprint[0]);matrix=np.column_stack([(np.asarray(footprint[1])-origin)/(w-1),(np.asarray(footprint[3])-origin)/(h-1)])
            if abs(np.linalg.det(matrix))<1e-12:matrix=None
        for row in rows:
            if row.get('chip_id')!=tile.get('chip_id'):continue
            stage=row.get('stage_polygon_um')
            same=row.get('source_image')==tile['source']
            if same:polygon=row['polygon_px']
            elif stage and matrix is not None:polygon=_clip_polygon((np.asarray(stage)-origin)@np.linalg.inv(matrix).T,w,h)
            else:continue
            if len(polygon)<3:continue
            result.append({**row,'id':row['id']+'@'+tile['key'],'catalog_id':row['id'],
                'tile_key':tile['key'],'tile_id':tile['tile_id'],'source_image':tile['source'],
                'polygon_px':np.asarray(polygon).tolist(),'holes_px':row.get('holes_px',[]) if same else [],
                'observation_kind':'catalog_source' if same else 'projected_catalog_mask'})
    return result

def pack_contrast_rg(values,path):
    """Portable PNG: two 12-bit numerical contrasts, no colour-profile transform."""
    values=np.asarray(values)
    if values.ndim!=3 or values.shape[2]!=2:raise ValueError('Expected HxWx2 R/G contrast map.')
    valid=np.isfinite(values).all(axis=2)
    codes=np.rint((np.clip(np.nan_to_num(values),-2,2)+2)/4*4094).astype(np.uint32)+1
    packed=(codes[:,:,0]<<12)|codes[:,:,1];packed[~valid]=0
    rgb=np.stack([(packed>>16)&255,(packed>>8)&255,packed&255],axis=-1).astype(np.uint8)
    Image.fromarray(rgb).save(path)
    return {'packing':'r12g12_rgb8','range':[-2.,2.],'levels':4094,'shape_hw':list(values.shape[:2]),'invalid_code':0}

def _review_maps(folder,assets,slug,tile):
    filename=tile.get('contrast_rg_file')
    if filename:
        path=(folder/filename).resolve()
        if not path.is_relative_to(folder):raise ValueError('Contrast map escapes analysis folder.')
        if path.exists():
            with np.load(path,allow_pickle=False) as data:
                name=f'review-assets/contrast-{slug}.png';description=pack_contrast_rg(data['contrast_rg'],folder/name)
                description.update(image=name,source_shape_hw=np.asarray(data.get('original_shape_hw',tile['shape'])).tolist(),
                    purpose='Display-only raw-pixel R/G selection; independent of proposal masks and catalog area cutoff.')
                tile['contrast_preview']=description
    filename=tile.get('instance_mask') or tile.get('instance_labels_file') or tile.get('instance_label_file') or tile.get('instance_mask_file')
    if filename:
        path=(folder/filename).resolve()
        if not path.is_relative_to(folder):raise ValueError('Instance map escapes analysis folder.')
        labels=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)
        if labels is not None and labels.ndim==2:
            edge=np.zeros(labels.shape,bool)
            edge[:,1:]|=labels[:,1:]!=labels[:,:-1];edge[:,:-1]|=labels[:,:-1]!=labels[:,1:]
            edge[1:]|=labels[1:]!=labels[:-1];edge[:-1]|=labels[:-1]!=labels[1:]
            edge[0]=edge[-1]=True;edge[:,0]=edge[:,-1]=True;edge&=labels>0
            palette=np.array([[118,228,183],[104,183,255],[228,174,255],[255,193,122],[248,135,167],[179,204,118]],np.uint8)
            rgba=np.zeros((*labels.shape,4),np.uint8);rgba[:,:,:3]=palette[labels%len(palette)];rgba[:,:,3]=np.where(edge,255,0)
            name=f'review-assets/instances-{slug}.png';Image.fromarray(rgba).save(folder/name)
            tile['instance_boundary_image']=name
            tile['instance_label_count']=int(len(np.unique(labels[labels>0])))

def crop_bounds(row,shape,pixel_size_um,field_um=70):
    h,w=shape[:2];poly=np.asarray(row['polygon_px'])
    centre=np.asarray(row['centroid_px'])
    size=max(field_um/min(pixel_size_um),float(np.ptp(poly,axis=0).max())*1.8,64)
    size=min(int(np.ceil(size)),max(h,w))
    x0=max(0,min(int(centre[0]-size/2),w-size));y0=max(0,min(int(centre[1]-size/2),h-size))
    return [x0,y0,min(w,x0+size),min(h,y0+size)]

def refresh_review(folder,asset_source):
    """Refresh classifications without decoding raw tiles or rebuilding crops.

    Reuse is allowed only for the same source pixels, row geometry and exact
    instance masks. Assets are copied, not linked: future review builds must
    never mutate the source gallery through shared files.
    """
    folder=Path(folder).resolve();asset_source=Path(asset_source).resolve()
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    previous=read(asset_source/'review-data.json');payload=read(folder/'flakes.json')
    summary=read(folder/'analysis.json');observation_payload=read(folder/'observations.json')
    old_payload=read(asset_source/'flakes.json')
    if (Path(payload['source_folder']).resolve()!=Path(old_payload['source_folder']).resolve()
            or payload.get('dataset_id')!=previous.get('dataset_id')):
        raise ValueError('Review asset reuse requires the identical source dataset.')
    rows=payload['flakes'];old_rows={r['id']:r for r in previous['flakes']}
    if len(rows)!=len(old_rows) or {r['id'] for r in rows}!=set(old_rows):
        raise ValueError('Review asset reuse requires unchanged object identities.')
    for row in rows:
        old=old_rows[row['id']]
        for key in ('source_image','tile_key','polygon_px','holes_px','centroid_px','instance_label'):
            if row.get(key)!=old.get(key):raise ValueError('Review geometry changed: '+row['id'])
        for key in ('crop_image','tile_image','crop_bounds_px','image_shape','pixel_size_um'):
            if key in old:row[key]=old[key]
    old_tiles={t['key']:t for t in previous['summary']['tiles']}
    if {t['key'] for t in summary['tiles']}!=set(old_tiles):raise ValueError('Review source fields changed.')
    for tile in summary['tiles']:
        old=old_tiles[tile['key']]
        for key in ('source','sha256','shape','instance_mask'):
            if tile.get(key)!=old.get(key):raise ValueError('Review source pixels changed: '+tile['key'])
        mask=tile.get('instance_mask')
        if mask:
            new_path=(folder/mask).resolve();old_path=(asset_source/mask).resolve()
            if not new_path.is_relative_to(folder) or not old_path.is_relative_to(asset_source):
                raise ValueError('Instance mask escapes analysis folder.')
            if hashlib.sha256(new_path.read_bytes()).digest()!=hashlib.sha256(old_path.read_bytes()).digest():
                raise ValueError('Instance mask changed: '+tile['key'])
        for key in ('image','context','metadata','pixel_size_um','contrast_preview','instance_boundary_image','instance_label_count'):
            if key in old:tile[key]=old[key]
    observations=observation_payload['observations']
    old_observations={r['id']:r for r in previous['observations']}
    if len(observations)!=len(old_observations):raise ValueError('Review observations changed.')
    for row in observations:
        old=old_observations.get(row['id'])
        if old is None or any(row.get(k)!=old.get(k) for k in ('source_image','polygon_px','holes_px','instance_label')):
            raise ValueError('Review observation geometry changed: '+row['id'])
    for tile in summary['tiles']:
        tile_rows=[r for r in observations if r.get('tile_key')==tile['key']]
        tile['observation_count']=len(tile_rows)
        tile['useful_observation_count']=sum(r.get('area_um2',0)>=100 and r.get('disposition')!='rejected' for r in tile_rows)
    data={**previous,'flakes':rows,'observations':observations,'summary':summary,
          'physical_unique_count':observation_payload.get('physical_unique_count',previous.get('physical_unique_count')),
          'review_status':'unvalidated','asset_source':str(asset_source)}
    for review_file in (folder/'reviews.json',asset_source/'reviews.json'):
        if review_file.exists():
            old=read(review_file)
            if old.get('dataset_id')==data['dataset_id']:
                data['annotations']=old.get('annotations',{});data['rois']=old.get('rois',{})
                break
    if folder!=asset_source:shutil.copytree(asset_source/'review-assets',folder/'review-assets',dirs_exist_ok=True)
    return _write_review(folder,data)

def _write_review(folder,data):
    profile_path=folder/'material-profile.json'
    if profile_path.is_file():
        profile=json.loads(profile_path.read_text(encoding='utf-8'))
        model_id=data['summary'].get('material_selection',{}).get('model_id')
        if profile.get('model_id')!=model_id:raise ValueError('Review material profile identity mismatch.')
        data['summary']['material_selection_model']={key:profile[key] for key in
            ('model_id','components','parameters','qualification','kind','approximate_reference_rg','source_qualification') if key in profile}
    atomic_json(folder/'review-data.json',data)
    template=(Path(__file__).parent/'review.html').read_text(encoding='utf-8')
    encoded=json.dumps(data,ensure_ascii=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    temporary=folder/'review.html.tmp'
    temporary.write_text(template.replace('__REVIEW_DATA__',encoded),encoding='utf-8')
    temporary.replace(folder/'review.html')
    return folder/'review.html'

def build_review(folder):
    folder=Path(folder).resolve()
    payload=json.loads((folder/'flakes.json').read_text()) if (folder/'flakes.json').exists() else {'flakes':[],'source_folder':str(folder)}
    summary=json.loads((folder/'analysis.json').read_text()) if (folder/'analysis.json').exists() else {}
    source=Path(payload.get('source_folder',folder)).resolve()
    assets=folder/'review-assets';assets.mkdir(exist_ok=True)
    rows=payload['flakes'];by_source={}
    observation_payload=json.loads((folder/'observations.json').read_text(encoding='utf-8')) if (folder/'observations.json').exists() else None
    observations=observation_payload.get('observations',[]) if isinstance(observation_payload,dict) else observation_payload or []
    for row in rows:
        relative=row.get('source_image')
        if relative is None:
            candidate=Path(row.get('chip_id') or '')/'tiles'/(row['tile_id']+'.tiff')
            relative=candidate.as_posix() if (source/candidate).exists() else 'tiles/'+row['tile_id']+'.tiff'
        path=(source/relative).resolve()
        if not path.is_relative_to(source):raise ValueError('Candidate source escapes scan folder.')
        row['source_image']=relative
        by_source.setdefault(path,[]).append(row)
    tiles=summary.setdefault('tiles',[])
    # Raw scans can be inspected and annotated before a detector has run.
    contexts={}
    manifests=[source/'manifest.json'] if (source/'manifest.json').exists() else sorted(source.glob('*/manifest.json'))
    for manifest_path in manifests:
        manifest=json.loads(manifest_path.read_text());context=manifest.get('context',{})
        for name in manifest.get('completed',{}):
            relative=(manifest_path.parent/'tiles'/(name+'.tiff')).relative_to(source).as_posix()
            contexts[relative]=context
    if not tiles and manifests:
        from .analysis import tile_inputs
        from dataclasses import asdict
        for item in tile_inputs(source):
            frame=load_frame(item['path']);h,w=frame.rgb.shape[:2]
            tiles.append({'key':item['key'],'tile_id':item['tile_id'],'chip_id':item['chip_id'],
                'source':item['path'].relative_to(source).as_posix(),'position':asdict(frame.position),
                'shape':[h,w],'pixel_size_um':list(frame.pixel_size_um),
                'sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest(),
                'footprint_um':[item['transform'].stage_xy(p,frame).tolist() for p in [[0,0],[w-1,0],[w-1,h-1],[0,h-1]]]})
    # Include fields with no proposals in the map and tile browser.
    for tile in tiles:by_source.setdefault((source/tile['source']).resolve(),[])
    for observation in observations:
        relative=observation.get('source_image')
        if relative:
            path=(source/relative).resolve()
            if not path.is_relative_to(source):raise ValueError('Observation source escapes scan folder.')
            by_source.setdefault(path,[])
    for path,items in by_source.items():
        if not path.is_relative_to(source):raise ValueError('Tile source escapes scan folder.')
        frame=load_frame(path);rgb=preview(frame.rgb)
        slug=hashlib.sha256(str(path.relative_to(source)).encode()).hexdigest()[:16]
        tile_name=f'review-assets/tile-{slug}.jpg'
        Image.fromarray(rgb).save(folder/tile_name,quality=92)
        relative=path.relative_to(source).as_posix()
        # Legacy galleries may contain proposals without an analysis tile table.
        if not any(t['source']==relative for t in tiles):
            exemplar=items[0] if items else next((o for o in observations if o.get('source_image')==relative),{})
            tile_id=exemplar.get('tile_id',path.stem)
            tiles.append({'key':exemplar.get('tile_key',tile_id),'tile_id':tile_id,'chip_id':exemplar.get('chip_id'),
                'source':relative,'position':{'x':frame.position.x,'y':frame.position.y,'z':frame.position.z},
                'shape':list(rgb.shape[:2]),'pixel_size_um':list(frame.pixel_size_um),
                'sha256':hashlib.sha256(frame.rgb.tobytes()).hexdigest()})
        for tile in tiles:
            if tile['source']==relative:
                tile.update(image=tile_name,context=contexts.get(relative,{}),metadata=frame.metadata,
                            shape=list(rgb.shape[:2]),pixel_size_um=list(frame.pixel_size_um))
                _review_maps(folder,assets,slug,tile)
        for row in items:
            x0,y0,x1,y1=crop_bounds(row,rgb.shape,frame.pixel_size_um)
            crop=rgb[y0:y1,x0:x1];name=f'review-assets/{row["id"]}.jpg'
            im=Image.fromarray(crop);im.thumbnail((480,480));im.save(folder/name,quality=94)
            row.update(crop_image=name,tile_image=tile_name,crop_bounds_px=[x0,y0,x1,y1],
                       image_shape=list(rgb.shape[:2]),pixel_size_um=list(frame.pixel_size_um))
    if observation_payload is None:observations=catalog_projections(rows,tiles)
    for tile in tiles:
        tile_rows=[r for r in observations if r.get('source_image')==tile['source'] or r.get('tile_key')==tile['key']]
        tile['observation_count']=len(tile_rows)
        tile['useful_observation_count']=sum(r.get('area_um2',0)>=100 and r.get('disposition')!='rejected' for r in tile_rows)
    data={'flakes':rows,'observations':observations,'observation_source':'per_field_detections' if observation_payload is not None else 'catalog_projections',
          'physical_unique_count':observation_payload.get('physical_unique_count',sum(r.get('disposition')!='rejected' for r in rows)) if isinstance(observation_payload,dict) else sum(r.get('disposition')!='rejected' for r in rows),
          'summary':summary,'dataset_id':payload.get('dataset_id',hashlib.sha256(str(source).encode()).hexdigest()[:20]),
          'title':source.name,'annotations':{},'rois':{},'default_min_area_um2':100,
          'review_status':'unvalidated','preview_description':'Display gamma 0.6; each tile is scaled from its 99.7th percentile. Raw TIFF values are unchanged.'}
    if (folder/'validation-status.json').exists():
        status=json.loads((folder/'validation-status.json').read_text(encoding='utf-8'))
        data['review_status']=status.get('status','unvalidated')
        data['validation_note']=status.get('message','')
    if (folder/'reviews.json').exists():
        old=json.loads((folder/'reviews.json').read_text())
        if old.get('dataset_id')==data['dataset_id']:
            data['annotations']=old.get('annotations',{});data['rois']=old.get('rois',{})
    elif source!=folder and (source/'reviews.json').exists():
        # A new detector result must not discard independent raw-image labels.
        # Inherit only annotations whose recorded source pixels still match.
        old=json.loads((source/'reviews.json').read_text())
        tile_hashes={t['key']:t.get('sha256') for t in tiles}
        data['rois']={key:r for key,r in old.get('rois',{}).items()
            if r.get('source_sha256') and r.get('source_sha256')==tile_hashes.get(r.get('tile_key'))}
    return _write_review(folder,data)


def validate_reviews(body,data):
    """Validate browser coordinates and derive physical provenance on the server."""
    if body.get('dataset_id')!=data['dataset_id']:raise ValueError('Dataset mismatch')
    annotations=body.get('annotations',{});rois=body.get('rois',{})
    if not isinstance(annotations,dict) or not isinstance(rois,dict):raise ValueError('Invalid annotations')
    ids={r['id'] for r in data['flakes']};tiles={t['key']:t for t in data['summary']['tiles']};clean={};clean_rois={}
    for key,v in annotations.items():
        if key not in ids or not isinstance(v,dict):raise ValueError('Unknown candidate')
        if v.get('decision') not in ('keep','reject','uncertain','unreviewed'):raise ValueError('Invalid decision')
        note=v.get('note','')
        if not isinstance(note,str) or len(note)>2000:raise ValueError('Invalid note')
        clean[key]={'decision':v['decision'],'note':note}
    if len(rois)>10000:raise ValueError('Too many regions')
    for key,v in rois.items():
        if not isinstance(key,str) or not re.fullmatch(r'roi-[a-zA-Z0-9-]{1,80}',key) or not isinstance(v,dict):raise ValueError('Invalid ROI')
        tile=tiles.get(v.get('tile_key'));kind=v.get('label');note=v.get('note','');points=v.get('polygon_px')
        if tile is None or kind not in ('flake','background','debris','uncertain'):raise ValueError('Invalid ROI source or class')
        geometry=v.get('geometry','polygon')
        if geometry not in ('rectangle','polygon'):raise ValueError('Invalid ROI geometry')
        if not isinstance(note,str) or len(note)>2000:raise ValueError('Invalid note')
        if not isinstance(points,list) or not 3<=len(points)<=256:raise ValueError('Invalid polygon')
        h,w=tile['shape']
        if any(not isinstance(p,list) or len(p)!=2 or any(not isinstance(c,(int,float)) or isinstance(c,bool) or not math.isfinite(c) for c in p) or not (0<=p[0]<=w-1 and 0<=p[1]<=h-1) for p in points):raise ValueError('Invalid polygon coordinates')
        polygon=np.asarray(points,dtype=np.float64)
        area=abs(float(np.dot(polygon[:,0],np.roll(polygon[:,1],-1))-np.dot(polygon[:,1],np.roll(polygon[:,0],-1))))/2
        if area<4:raise ValueError('ROI is too small')
        physical=None
        footprint=tile.get('footprint_um')
        if footprint and len(footprint)==4 and w>1 and h>1:
            origin=np.asarray(footprint[0]);horizontal=(np.asarray(footprint[1])-origin)/(w-1);vertical=(np.asarray(footprint[3])-origin)/(h-1)
            physical=(origin+polygon[:,0,None]*horizontal+polygon[:,1,None]*vertical).tolist()
        clean_rois[key]={'id':key,'tile_key':v['tile_key'],'source_image':tile['source'],'source_sha256':tile.get('sha256'),
            'label':kind,'polygon_px':points,'stage_polygon_um':physical,'area_um2':area*float(np.prod(tile['pixel_size_um'])),
            'note':note,'annotation_type':'manual_visual_roi','geometry':geometry,'layer_number':None}
    return {'schema':2,'dataset_id':data['dataset_id'],'annotations':clean,'rois':clean_rois}

def serve_review(folder,port=0,open_browser=False):
    """Loopback-only reviewer. Writes are restricted to validated review annotations."""
    from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
    from urllib.parse import urlsplit,unquote
    import mimetypes, secrets, threading, webbrowser
    folder=Path(folder).resolve();build_review(folder)
    data=json.loads((folder/'review-data.json').read_text())
    token=secrets.token_urlsafe(32);lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,content,kind='application/json'):
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
        def valid_host(self):return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'
        def do_GET(self):
            if not self.valid_host():return self.reply(403,b'{}')
            url=urlsplit(self.path).path
            if url=='/api/session':return self.reply(200,json.dumps({'token':token}).encode())
            if url=='/api/reviews':
                path=folder/'reviews.json'
                return self.reply(200,path.read_bytes() if path.exists() else json.dumps({'dataset_id':data['dataset_id'],'annotations':{},'rois':{}}).encode())
            relative=unquote(url).lstrip('/') or 'review.html';path=(folder/relative).resolve()
            if not path.is_relative_to(folder) or not path.is_file() or path.suffix not in ('.html','.jpg','.png','.json','.csv'):
                return self.reply(404,b'{}')
            self.reply(200,path.read_bytes(),mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        def do_POST(self):
            origin=self.headers.get('Origin')
            if not self.valid_host() or origin not in (None,f'http://127.0.0.1:{self.server.server_port}') or self.headers.get('X-Review-Token')!=token:
                return self.reply(403,b'{}')
            if self.path!='/api/reviews':return self.reply(404,b'{}')
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<2_000_000:raise ValueError('Invalid request size')
                body=json.loads(self.rfile.read(size))
                clean=validate_reviews(body,data)
                with lock:atomic_json(folder/'reviews.json',clean)
                self.reply(200,b'{"saved":true}')
            except (ValueError,KeyError,TypeError):self.reply(400,b'{"error":"Invalid review data"}')
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}/'
    if open_browser:webbrowser.open(url)
    return server,url
