"""EXPERIMENTAL, isolated normalization using saved acquisition support.

No production caller imports this module. The analysis domain is the completed
raster's measured enclosure, not the color-selected substrate sample mask.
Thus differently colored flakes inside the chip remain eligible for extraction.
"""
from audit_saved_outputs import *

def robust_coefficients(X,Y,base):
    valid=base.copy()
    if valid.sum()<100:raise ValueError('Insufficient recorded substrate samples; do not normalize this field.')
    for _ in range(6):
        coeff=np.linalg.lstsq(X[valid],Y[valid],rcond=None)[0]
        pred=X@coeff
        residual=np.linalg.norm((Y-pred)/np.maximum(pred,1),axis=1)
        median=np.median(residual[valid]);mad=max(1.4826*np.median(np.abs(residual[valid]-median)),.001)
        update=base&(residual<median+2.5*mad)
        if update.sum()<100:break
        valid=update
    return coeff,valid

def normalize_observed(frame,manifest,labels,label_sha256,tile_id,edge_margin_um=12):
    """Return explicit valid/background-fit masks, RGB contrast, and provenance.

    Outside-domain arrays are NaN intentionally: an integration must carry the
    valid mask through every subsequent operation. Nothing is inferred off-chip.
    """
    recorded=manifest['completed'][tile_id]['hashes']['boundary-labels/'+tile_id+'.npz']
    if recorded!=label_sha256:raise ValueError('Boundary-label checksum mismatch.')
    context=manifest['context'];metadata=frame.metadata
    checks=((metadata['camera_resources']['ff_exposure_ms'],context['exposure_ms']),
      (metadata['camera_resources']['ff_white_balance'],context['white_balance']),
      (metadata['objective']['name'],context['objective']),
      (metadata['illumination_percent'],context['illumination_percent']),
      (metadata['white_level'],context['white_level']))
    if any(a!=b for a,b in checks):raise ValueError('Raw frame optical profile differs from acquisition context.')
    actual=manifest['completed'][tile_id]['position']
    if max(abs(actual[k]-getattr(frame.position,k)) for k in ('x','y','z'))>1e-6:
        raise ValueError('Frame position differs from saved boundary evidence.')
    enclosure=manifest['boundary_validation']['acquired_raster_evidence']
    if enclosure.get('status')!='enclosed':raise ValueError('No measured enclosed footprint is available.')
    h,w=frame.rgb.shape[:2];matrix=np.asarray(manifest['transform']['matrix'])
    polygon=(np.asarray(enclosure['outline_um'])-[frame.position.x,frame.position.y])@np.linalg.inv(matrix).T+[(w-1)/2,(h-1)/2]
    valid=np.zeros((h,w),np.uint8);cv2.fillPoly(valid,[np.int32(np.round(polygon))],1)
    radius=int(np.ceil(edge_margin_um/min(frame.pixel_size_um)))
    valid=cv2.erode(valid,np.ones((2*radius+1,2*radius+1),np.uint8)).astype(bool)
    if valid.sum()<1000:raise ValueError('No sufficient measured on-chip interior; keep excluded.')
    support=cv2.resize(np.uint8(labels==1),(w,h),interpolation=cv2.INTER_NEAREST)
    # Modest clearance from uncertain/classified feature pixels; do not erode
    # every classification hole by the much larger chip-edge exclusion margin.
    clearance=int(np.ceil(1/min(frame.pixel_size_um)))
    support=cv2.erode(support,np.ones((2*clearance+1,2*clearance+1),np.uint8)).astype(bool)&valid
    stride=max(1,int(np.sqrt(h*w/30000)))
    xx,yy=np.meshgrid(np.linspace(-1,1,w)[::stride],np.linspace(-1,1,h)[::stride])
    X=np.stack([np.ones_like(xx),xx,yy,xx*xx,xx*yy,yy*yy],-1).reshape(-1,6)
    Y=np.asarray(frame.rgb[::stride,::stride],np.float32).reshape(-1,3)
    base=support[::stride,::stride].reshape(-1)
    # Four spatially interleaved held-out block groups. Use the simplest model
    # within one standard error of the best measured validation score.
    ix=np.minimum(((xx+1)*2).astype(int),3);iy=np.minimum(((yy+1)*2).astype(int),3)
    folds=((ix+2*iy)%4).reshape(-1)
    candidates=[]
    for degree,columns in ((0,1),(1,3),(2,6)):
        scores=[]
        for fold in range(4):
            train=base&(folds!=fold);test=base&(folds==fold)
            if train.sum()<100 or test.sum()<25:continue
            coeff,_=robust_coefficients(X[:,:columns],Y,train)
            pred=X[test,:columns]@coeff
            if np.any(pred<=0):scores.append(float('inf'));continue
            scores.append(float(np.median(np.linalg.norm((Y[test]-pred)/pred,axis=1))))
        if len(scores)>=3 and np.isfinite(scores).all():
            candidates.append({'degree':degree,'columns':columns,'fold_scores':scores,'mean':float(np.mean(scores)),
              'standard_error':float(np.std(scores,ddof=1)/np.sqrt(len(scores)))})
    if not candidates:raise ValueError('Insufficient spatial support for held-out normalization validation.')
    best=min(candidates,key=lambda x:x['mean']);threshold=best['mean']+best['standard_error']
    chosen=next(c for c in candidates if c['mean']<=threshold)
    coeff,retained=robust_coefficients(X[:,:chosen['columns']],Y,base)
    x=np.linspace(-1,1,w,dtype=np.float32)[None,:];y=np.linspace(-1,1,h,dtype=np.float32)[:,None]
    padded=np.zeros((6,3),np.float32);padded[:len(coeff)]=coeff
    background=np.full(frame.rgb.shape,np.nan,np.float32)
    for channel in range(3):
        c=padded[:,channel];plane=c[0]+c[1]*x+c[2]*y+c[3]*x*x+c[4]*x*y+c[5]*y*y
        background[:,:,channel][valid]=plane[valid]
    if np.any(background[valid]<=0):raise ValueError('Nonpositive background on measured chip interior; reject fit.')
    contrast=np.full(frame.rgb.shape,np.nan,np.float32)
    contrast[valid]=(background[valid]-frame.rgb[valid])/background[valid]
    record={'method':'experimental_observed_enclosure_and_recorded_substrate_normalization',
      'label_sha256':label_sha256,'enclosure_sha256':hashlib.sha256(json.dumps(enclosure,sort_keys=True).encode()).hexdigest(),
      'source_position':actual,'source_optics':manifest['context'],'edge_margin_um':edge_margin_um,
      'background_support_clearance_um':1.,'domain_fraction':float(valid.mean()),'background_support_fraction':float(support.mean()),
      'background_samples':int(base.sum()),'retained_background_samples':int(retained.sum()),
      'model_selection':'four spatial block groups, simplest within one standard error of lowest validation error',
      'models':candidates,'chosen_degree':chosen['degree'],'coefficients':coeff.tolist(),
      'minimum_background_inside_domain_rgb':background[valid].min(axis=0).tolist(),
      'domain_does_not_require_substrate_color':True,'outside_domain':'invalid / NaN; never clamp or interpret',
      'qualification':'Isolated normalization check; not a validated flake or thin-layer detector.'}
    return contrast,background,valid,support,record

def main():
    manifest=read(CHIP/'manifest.json');rows=[]
    for number in (0,1,7,32,180,354):
        tile=f'tile_{number:06d}';frame=load_frame(CHIP/'tiles'/f'{tile}.tiff')
        for suffix in ('.tiff','.json'):
            source=CHIP/'tiles'/(tile+suffix)
            if sha(source)!=manifest['completed'][tile]['hashes']['tiles/'+tile+suffix]:
                raise ValueError('Acquisition-hashed raw source changed.')
        path=CHIP/'boundary-labels'/f'{tile}.npz';labels=np.load(path)['labels']
        try:
            contrast,background,valid,support,record=normalize_observed(frame,manifest,labels,sha(path),tile)
            rows.append({'tile_id':tile,'status':'normalized','raw_source_hashes_verified':True,**record})
            small=cv2.resize(preview(frame.rgb,4095.),(768,641));border=cv2.resize(np.uint8(valid),(768,641),interpolation=cv2.INTER_NEAREST)
            contours,_=cv2.findContours(border,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(small,contours,-1,(255,80,80),1)
            field=cv2.resize(np.nan_to_num(contrast[:,:,1],nan=0),(768,641))
            heat=cv2.applyColorMap(np.uint8(np.clip(.5+field/.4,0,1)*255),cv2.COLORMAP_TURBO)[:,:,::-1];heat[border==0]=0
            sheet=Image.new('RGB',(1536,690),'#202020');sheet.paste(panel(Image.fromarray(small),tile+' raw: measured footprint in red',(768,690)),(0,0));sheet.paste(panel(Image.fromarray(heat),'Green contrast [-.2,+.2]; invalid exterior black',(768,690)),(768,0));sheet.save(OUT/(tile+'-normalization-repair.jpg'),quality=92)
        except ValueError as exc:rows.append({'tile_id':tile,'status':'excluded','reason':str(exc)})
    write('normalization-repair-results.json',rows)
    print(json.dumps([{k:r.get(k) for k in ('tile_id','status','reason','domain_fraction','background_support_fraction','chosen_degree','minimum_background_inside_domain_rgb')} for r in rows],indent=2))
if __name__=='__main__':main()
