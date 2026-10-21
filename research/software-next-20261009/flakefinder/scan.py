from __future__ import annotations
from dataclasses import dataclass,asdict
from pathlib import Path
import csv, json, math, hashlib, shutil, time
import cv2, numpy as np
from .vendor_lifecycle import checkpoint_observation,acknowledge_raw
from .model import Position, Frame, SafetyError, AcquisitionError, Cancelled, CameraTransform, atomic_json
from .imaging import save_frame,load_frame,preview
from .autofocus import autofocus,focus_metrics
from .segmentation import segment_flakes
from .chips import detect_chips
from .calibration import FocusMap

@dataclass
class ScanPlan:
    bounds_um: list[float]  # x_min, x_max, y_min, y_max: specimen region to cover
    overlap: float = 0.15
    autofocus_every: int = 5
    focus_half_range_um: float = 12
    focus_step_um: float = 3
    max_tiles: int = 10000
    chip_id: str | None = None
    polygon_um: list | None = None
    mode: str = 'detail'
    focus_strategy: str = 'periodic'
    focus_search_bounds_um: list[float] | None = None
    focus_max_residual_um: float = 1.0
    focus_initial_recovery: bool = False

    def __post_init__(self):
        if len(self.bounds_um)!=4 or not np.isfinite(self.bounds_um).all(): raise ValueError('Invalid scan bounds.')
        x0,x1,y0,y1=self.bounds_um
        if x0>=x1 or y0>=y1: raise ValueError('Scan bounds must increase.')
        if not 0.05<=self.overlap<=0.5: raise ValueError('Overlap must be between 0.05 and 0.5.')
        if self.autofocus_every<0 or self.max_tiles<1: raise ValueError('Invalid scan sampling parameters.')
        if self.mode not in ('detail','survey'): raise ValueError('Scan mode must be survey or detail.')
        if self.focus_strategy not in ('periodic','surface'): raise ValueError('Unknown focus strategy.')

def tile_centres(plan,frame,transform):
    transform.validate_frame(frame)
    inv=np.linalg.inv(transform.matrix)
    h,w=frame.rgb.shape[:2]
    # Use a stage-aligned rectangle fully contained in the camera footprint even
    # for rotated/mirrored cameras. This guarantees coverage with overlap.
    aspect=w/h
    ry=min((w-1)/2/(abs(inv[0,0])*aspect+abs(inv[0,1])),
           (h-1)/2/(abs(inv[1,0])*aspect+abs(inv[1,1])))
    rx=aspect*ry
    width,height=2*rx,2*ry
    x0,x1,y0,y1=plan.bounds_um
    def count(a,b,size):
        return 1 if b-a<=size else math.ceil((b-a-size)/(size*(1-plan.overlap)))+1
    nx,ny=count(x0,x1,width),count(y0,y1,height)
    # Check before linspace/list allocation and before testing a large polygon
    # bounding box. Polygon filtering cannot license unbounded planning work.
    candidate_budget=min(1000000,max(100000,plan.max_tiles*16)) if plan.polygon_um is not None else plan.max_tiles
    if nx*ny>candidate_budget:raise SafetyError('Raster candidate lattice exceeds its planning budget; narrow the bounds or use a coarser survey.')
    def centres(a,b,size,n):
        return [(a+b)/2] if n==1 else np.linspace(a+size/2,b-size/2,n).tolist()
    xs,ys=centres(x0,x1,width,nx),centres(y0,y1,height,ny)
    polygon=None if plan.polygon_um is None else np.asarray(plan.polygon_um,dtype=np.float32)
    result=[]
    for row,y in enumerate(ys):
        for x in xs if row%2==0 else reversed(xs):
            if polygon is not None:
                # Include tiles intersecting the polygon rather than centres only.
                rect=np.float32([[x-rx,y-ry],[x+rx,y-ry],[x+rx,y+ry],[x-rx,y+ry]])
                area,_=cv2.intersectConvexConvex(cv2.convexHull(polygon),rect)
                if area<=0: continue
            result.append((float(x),float(y)))
            if len(result)>plan.max_tiles:raise SafetyError(f'Tiles exceed the configured maximum {plan.max_tiles}.')
    if len(result)>plan.max_tiles: raise SafetyError(f'{len(result)} tiles exceeds the configured maximum {plan.max_tiles}.')
    return result

class Mosaic:
    def __init__(self,bounds_um,pixel_size_um=4,max_pixels=30000000):
        self.x0,self.x1,self.y0,self.y1=bounds_um
        self.scale=float(pixel_size_um)
        w=int(np.ceil((self.x1-self.x0)/self.scale))+1
        h=int(np.ceil((self.y1-self.y0)/self.scale))+1
        if self.scale<=0 or w*h>max_pixels or min(w,h)<=0: raise ValueError('Overview size is invalid or exceeds memory budget.')
        from .acquisition_resources import check_memory_headroom
        # Accumulator16 B/pixel plus RGB/preview/encoding working arrays.
        self.memory_preflight=check_memory_headroom(w*h*48,purpose='overview allocation and export')
        self.sum=np.zeros((h,w,3),np.float32)
        self.weight=np.zeros((h,w),np.float32)
        self._feather=None

    def add(self,f,transform):
        transform.validate_frame(f)
        h,w=f.rgb.shape[:2]
        matrix=np.asarray(transform.matrix)
        offset=np.array([f.position.x-self.x0,f.position.y-self.y0])-matrix@[(w-1)/2,(h-1)/2]
        affine=np.column_stack([matrix/self.scale,offset/self.scale])
        # Include the one-source-pixel bilinear border, then clip its transformed
        # bounding rectangle to the overview. Translation is in destination
        # pixels; the camera-to-stage geometry itself is unchanged.
        corners=np.array([[-1,-1],[w,-1],[w,h],[-1,h]])@affine[:,:2].T+affine[:,2]
        left,top=np.maximum(0,np.floor(corners.min(axis=0)-1).astype(int))
        right,bottom=np.minimum([self.weight.shape[1],self.weight.shape[0]],
            np.ceil(corners.max(axis=0)+2).astype(int))
        if right<=left or bottom<=top: return
        local=affine.copy(); local[:,2]-=[left,top]
        # A small nonzero edge weight ensures boundary pixels remain represented.
        if self._feather is None or self._feather.shape!=(h,w):
            self._feather=np.maximum(np.outer(np.hanning(h),np.hanning(w)),0.02).astype(np.float32)
        feather=self._feather
        shape=(int(right-left),int(bottom-top))
        self.sum[top:bottom,left:right]+=cv2.warpAffine(f.rgb.astype(np.float32)*feather[:,:,None],local,shape,flags=cv2.INTER_LINEAR)
        self.weight[top:bottom,left:right]+=cv2.warpAffine(feather,local,shape,flags=cv2.INTER_LINEAR)

    def rgb(self): return self.sum/np.maximum(self.weight[:,:,None],1e-8)
    def save(self,folder):
        folder=Path(folder)
        a=self.rgb()
        cv2.imwrite(str(folder/'overview.tiff'),np.uint16(np.clip(a,0,65535))[:,:,::-1])
        cv2.imwrite(str(folder/'overview.jpg'),preview(a)[:,:,::-1])
        cv2.imwrite(str(folder/'coverage.png'),np.uint8(self.weight>0)*255)
        atomic_json(folder/'overview.json',{'origin_um':[self.x0,self.y0],'pixel_size_um':self.scale,
            'coverage_fraction':float(np.mean(self.weight>0)),'bounds_um':[self.x0,self.x1,self.y0,self.y1]})

def run_scan(scope,plan,transform,folder,model=None,context=None,callback=None,resume=False):
    """Persist each tile atomically; resume skips only verified on-disk completed tiles."""
    if not scope.armed: raise SafetyError('Arm the microscope before scanning.')
    if not transform.calibrated: raise SafetyError('Calibrate camera direction before scanning.')
    from .storage import managed_output_path
    folder=managed_output_path(folder)
    if folder.exists() and any(folder.iterdir()) and not resume: raise ValueError('Output folder is not empty. Choose a new folder or explicit resume.')
    folder.mkdir(parents=True,exist_ok=True)
    origin=scope.position()
    x0,x1,y0,y1=plan.bounds_um
    for x,y in [(x0,y0),(x1,y1)]: scope.limits.check(Position(x,y,origin.z))
    initial=scope.capture(); transform.validate_frame(initial)
    centres=tile_centres(plan,initial,transform)
    payload={'schema':1,'plan':asdict(plan),'transform':asdict(transform),'limits':asdict(scope.limits),
        'context':context or {},'material_model':None if model is None else model.data,
        'acquisition_settings':{k:initial.metadata.get(k) for k in ('camera_job_template','camera_resources','objective_position','illumination_percent')},
        'motion_profile':{'xy_approach_um':getattr(scope,'xy_backlash_um',0)},
        'camera_shape':list(initial.rgb.shape),'source':initial.metadata.get('source','unknown')}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    manifest_path=folder/'manifest.json'
    if resume and manifest_path.exists():
        manifest=json.loads(manifest_path.read_text())
        if manifest['fingerprint']!=fingerprint: raise SafetyError('Resume configuration differs from the original scan.')
    else:
        manifest={**payload,'fingerprint':fingerprint,'started_at':time.time(),'origin':asdict(origin),'completed':{},'state':'running','tile_count':len(centres)}
    estimated=len(centres)*initial.rgb.nbytes*2
    if shutil.disk_usage(folder).free<estimated+100_000_000: raise OSError('Insufficient disk space for raw images and previews.')
    manifest['state']='running'; atomic_json(manifest_path,manifest)
    checkpoint_observation(scope,initial,folder/'preflight','scan_profile_verified',{'acquisition_settings':payload['acquisition_settings']})
    manifest.pop('error',None)
    all_flakes=[]
    focus_points=[]
    focus_surface=None
    mosaic=Mosaic(plan.bounds_um,max(max(initial.pixel_size_um)*4,(x1-x0)/3500,(y1-y0)/3500))
    try:
        if plan.focus_strategy=='surface' and len(manifest['completed'])<len(centres):
            from .focus_surface import prepare_focus_surface
            # Resume seeds belong to this exact acquisition fingerprint. Never
            # reuse heights from a different objective, sample or camera profile.
            seeds=[[v['position'][k] for k in ('x','y','z')]
                   for v in manifest['completed'].values() if (v.get('focus') or {}).get('success')]
            saved_surface=folder/'focus-surface'/'focus-surface.json'
            if resume and saved_surface.exists():
                saved=json.loads(saved_surface.read_text())
                # Manifest fingerprint validation above already established
                # the same sample/profile/plan. Reuse only verified anchors,
                # with their images and successful focus results preserved.
                for anchor in saved.get('anchors',[]):
                    if anchor.get('status')=='verified':seeds.append(anchor['position_um'])
                seeds.extend(saved.get('seed_points_um',[]))
            focus_surface,focus_points=prepare_focus_surface(scope,centres,
                {'half_range_um':plan.focus_half_range_um,'coarse_step_um':plan.focus_step_um,
                 'fine_step_um':max(plan.focus_step_um/6,.25),
                 'search_bounds_um':plan.focus_search_bounds_um,'max_residual_um':plan.focus_max_residual_um,
                 'recover_initial_unresolved':plan.focus_initial_recovery},
                folder/'focus-surface',callback,seed_points=seeds)
            manifest['focus_strategy']='surface' if focus_surface is not None else 'per_tile_autofocus'
            atomic_json(manifest_path,manifest)
        for i,(x,y) in enumerate(centres):
            scope.check_cancel()
            name=f'tile_{i:06d}'
            if name in manifest['completed']:
                f=load_frame(folder/'tiles'/(name+'.tiff'))
                transform.validate_frame(f)
                if max(abs(f.position.x-x),abs(f.position.y-y))>0.5: raise SafetyError('Saved tile position disagrees with the scan plan.')
                result=json.loads((folder/'tiles'/(name+'.flakes.json')).read_text())
                all_flakes.extend(result['flakes']); mosaic.add(f,transform)
                if manifest['completed'][name].get('focus',{}): focus_points.append([x,y,f.position.z])
                continue
            scope.travel(x,y)
            focus=None
            prediction=None
            if plan.focus_strategy=='surface' and focus_surface is not None:
                try: prediction=focus_surface.predict(x,y)
                except AcquisitionError: pass
            elif plan.focus_strategy=='periodic' and len(focus_points)>=4:
                try:
                    prediction=FocusMap(focus_points).predict(x,y)
                except (ValueError,AcquisitionError): prediction=None
            if prediction is not None:
                from .workflow import move_z_bounded
                move_z_bounded(scope,prediction)
            needs_focus=(prediction is None) if plan.focus_strategy=='surface' else bool(plan.autofocus_every and i%plan.autofocus_every==0)
            if needs_focus:
                focus=autofocus(scope,plan.focus_half_range_um,plan.focus_step_um,
                    max(plan.focus_step_um/6,0.25),folder/'focus'/name,callback=(lambda f,m: callback('focus',f,m)) if callback else None)
                if not focus.success: raise AcquisitionError(f'Autofocus failed at tile {i}: {focus.reason}')
                focus_points.append([x,y,focus.z_um])
            f=scope.capture(); transform.validate_frame(f)
            if {k:f.metadata.get(k) for k in payload['acquisition_settings']}!=payload['acquisition_settings']:
                raise SafetyError('Camera or illumination settings changed during the scan.')
            if focus_metrics(f.rgb)['score']<1e-6 and plan.mode=='detail':
                raise AcquisitionError(f'Tile {i} lacks resolvable texture; detail scan paused.')
            save_frame(folder/'tiles',name,f)
            acknowledge_raw(scope,f,folder/'tiles'/(name+'.tiff'))
            flakes=[]; quality={}
            if plan.mode=='detail':
                substrate_mask=None
                if plan.polygon_um is not None:
                    h,w=f.rgb.shape[:2]
                    polygon=(np.array(plan.polygon_um)-[f.position.x,f.position.y])@np.linalg.inv(transform.matrix).T+[(w-1)/2,(h-1)/2]
                    substrate_mask=np.zeros((h,w),np.uint8)
                    cv2.fillPoly(substrate_mask,[np.int32(np.round(polygon))],1)
                flakes,mask,quality=segment_flakes(f,transform,model=model,context=context,substrate_mask=substrate_mask)
                cv2.imwrite(str(folder/'tiles'/(name+'.mask.png')),mask)
            for flake in flakes:
                flake.id=f'{plan.chip_id or "survey"}-{name}-{flake.id}'
                flake.chip_id=plan.chip_id; flake.tile_id=name
            rows=[asdict(v) for v in flakes]
            atomic_json(folder/'tiles'/(name+'.flakes.json'),{'flakes':rows,'quality':quality})
            all_flakes.extend(rows); mosaic.add(f,transform)
            manifest['completed'][name]={'position':asdict(f.position),'sequence':f.sequence,'focus':None if focus is None else asdict(focus),
                'focus_prediction_um':prediction,'focus_method':'local_autofocus' if focus is not None else ('measured_surface' if plan.focus_strategy=='surface' and prediction is not None else 'previous_height')}
            atomic_json(manifest_path,manifest)
            if (i+1)%10==0:mosaic.save(folder)
            if callback: callback('tile',f,{'index':i+1,'total':len(centres),'flakes':len(flakes)})
        manifest['state']='complete'
    except BaseException as e:
        manifest['state']='stopped' if isinstance(e,Cancelled) else 'failed'
        manifest['error']=str(e)
        scope.stop()
        raise
    finally:
        atomic_json(manifest_path,manifest)
        mosaic.save(folder)
        atomic_json(folder/'flakes.json',{'flakes':deduplicate(all_flakes)})
        export_csv(folder/'flakes.csv',deduplicate(all_flakes))
    return manifest

def deduplicate(flakes):
    """Merge overlap observations only for close, comparable objects on different tiles."""
    accepted=[]
    for original in sorted(flakes,key=lambda f:(f['touches_border'],-f['area_um2'])):
        item=dict(original)
        xy=item.get('stage_xy_um')
        duplicate=False
        if xy is not None:
            for old in accepted:
                if old.get('chip_id')!=item.get('chip_id') or old.get('tile_id')==item.get('tile_id') or old.get('stage_xy_um') is None: continue
                distance=np.linalg.norm(np.array(xy)-old['stage_xy_um'])
                tolerance=min(3,max(0.8,0.12*np.sqrt(min(item['area_um2'],old['area_um2']))))
                area_ratio=min(item['area_um2'],old['area_um2'])/max(item['area_um2'],old['area_um2'])
                colour_distance=float(np.linalg.norm(np.array(item['contrast_rgb'])-old['contrast_rgb']))
                overlap_match=False
                if distance<5 and item.get('stage_polygon_um') and old.get('stage_polygon_um'):
                    # Subtract the global origin before float32 geometry so micron
                    # polygons keep precision at large absolute stage coordinates.
                    a=cv2.convexHull(np.float32(np.array(item['stage_polygon_um'])-xy))
                    b=cv2.convexHull(np.float32(np.array(old['stage_polygon_um'])-xy))
                    intersection,_=cv2.intersectConvexConvex(a,b)
                    smaller=min(cv2.contourArea(a),cv2.contourArea(b))
                    overlap_match=smaller>0 and intersection/smaller>.6 and area_ratio>.5 and colour_distance<.12
                if overlap_match or (distance<tolerance and area_ratio>0.65 and colour_distance<0.025):
                    old.setdefault('overlap_observations',[]).append({k:item.get(k) for k in ['id','tile_id','stage_xy_um','contrast_rgb','area_um2','label']})
                    old['max_repeat_contrast_distance']=max(old.get('max_repeat_contrast_distance',0),colour_distance)
                    if old.get('label')!=item.get('label'):old['label']='inconsistent_observations'
                    duplicate=True; break
        if not duplicate: accepted.append(item)
    return accepted

def export_csv(path,flakes):
    fields=['id','chip_id','tile_id','label','area_um2','x_um','y_um','contrast_r','contrast_g','contrast_b','uniformity','solidity','touches_border']
    with open(path,'w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader()
        for flake in flakes:
            row={k:flake.get(k) for k in fields if k in flake}
            xy=flake.get('stage_xy_um') or [None,None]
            row.update(x_um=xy[0],y_um=xy[1],contrast_r=flake['contrast_rgb'][0],contrast_g=flake['contrast_rgb'][1],contrast_b=flake['contrast_rgb'][2])
            writer.writerow(row)

def identify_chips(folder,expected_count=None,grid_shape=None,polarity='bright',min_area_um2=None):
    from .storage import managed_output_path
    folder=Path(folder)
    output=managed_output_path(folder)
    output.mkdir(parents=True,exist_ok=True)
    d=json.loads((folder/'overview.json').read_text())
    coverage=cv2.imread(str(folder/'coverage.png'),cv2.IMREAD_GRAYSCALE)
    if coverage is None or np.mean(coverage>0)<0.99:
        raise AcquisitionError('Overview coverage is incomplete; finish or expand the survey before assigning chips.')
    rgb=cv2.imread(str(folder/'overview.tiff'),cv2.IMREAD_UNCHANGED)[:,:,::-1]
    area=max(500,0.0005*rgb.shape[0]*rgb.shape[1]) if min_area_um2 is None else min_area_um2/d['pixel_size_um']**2
    if not np.isfinite(area) or area<=0:raise ValueError('Minimum chip area must be positive.')
    chips,mask=detect_chips(rgb,min_area_px=area,
        expected_count=expected_count,grid_shape=grid_shape,polarity=polarity)
    overlay=preview(rgb)
    for chip in chips:
        chip.stage_polygon_um=(np.array(chip.polygon_px)*d['pixel_size_um']+d['origin_um']).tolist()
        cv2.polylines(overlay,[np.int32(chip.polygon_px)],True,(0,255,100),2)
        cv2.putText(overlay,chip.id,tuple(np.int32(chip.centre_px)),cv2.FONT_HERSHEY_SIMPLEX,0.7,(255,255,255),2)
    cv2.imwrite(str(output/'chips.png'),overlay[:,:,::-1])
    cv2.imwrite(str(output/'chip-mask.png'),mask)
    atomic_json(output/'chips.json',{'chips':[asdict(c) for c in chips],'grid_shape':list(grid_shape) if grid_shape else None,
        'missing':[f'{chr(65+r)}{c+1}' for r in range(grid_shape[0]) for c in range(grid_shape[1]) if not any(x.row==r and x.column==c for x in chips)] if grid_shape else [],
        'coordinate_convention':'Rows increase along overview stage +Y; columns along +X.'})
    return chips
