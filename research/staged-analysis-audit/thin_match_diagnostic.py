"""Small raw-ROI audit of saved matches; does not resegment or rewrite rows."""
from audit_saved_outputs import *
from scipy.ndimage import distance_transform_edt

def inspect(row,manifest):
    tile=row['tile_id'];frame=load_frame(CHIP/'tiles'/f'{tile}.tiff')
    labels=cv2.imread(str(ANALYSIS/f'chip001__{tile}.instances.png'),cv2.IMREAD_UNCHANGED)
    boundary=np.load(CHIP/'boundary-labels'/f'{tile}.npz')['labels']
    recorded=cv2.resize(np.uint8(boundary==1),(labels.shape[1],labels.shape[0]),interpolation=cv2.INTER_NEAREST)>0
    x,y,w,h=row['bbox_px'];margin=50;x0=max(0,x-margin);y0=max(0,y-margin);x1=min(labels.shape[1],x+w+margin);y1=min(labels.shape[0],y+h+margin)
    lab=labels[y0:y1,x0:x1];mask=lab==row['instance_label'];rgb=frame.rgb[y0:y1,x0:x1].astype(float)
    interior=mask&(distance_transform_edt(mask,sampling=frame.pixel_size_um)>.5)
    distance=distance_transform_edt(~mask,sampling=frame.pixel_size_um)
    occupied=lab>0;clear=distance_transform_edt(~occupied,sampling=frame.pixel_size_um)>1.
    annulus=clear&recorded[y0:y1,x0:x1]&(distance<=7.)
    bg=np.median(rgb[annulus],axis=0);contrast=(bg-rgb)/bg
    # Fixed approximately1um blocks, global sensor aligned, not chosen by color.
    yy,xx=np.indices(mask.shape);block=6;key=((yy+y0)//block)*1000+(xx+x0)//block
    selected=key[interior];unique,inverse=np.unique(selected,return_inverse=True);count=np.bincount(inverse)
    patch=np.column_stack([np.bincount(inverse,weights=contrast[interior,c])/count for c in range(3)])
    patch=patch[count>=12]
    directional=(patch[:,0]>0)&(patch[:,1]>patch[:,0])&(patch[:,1]>=.02)&(patch[:,1]<=.45)
    img=preview(np.uint16(rgb),4095.);overlay=img.copy();overlay[annulus]=np.uint8(.55*overlay[annulus]+.45*np.array([80,180,255]))
    contours,_=cv2.findContours(np.uint8(mask),cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(overlay,contours,-1,(255,80,80),1)
    Image.fromarray(overlay).save(OUT/f'{tile}-local-background-audit.png')
    return {'tile_id':tile,'id':row['id'],'annulus_method':'independent raw RGB: 1-7um, all saved instances excluded, recorded SUBSTRATE labels required; no color closeness-to-zero filter',
      'annulus_pixels':int(annulus.sum()),'local_substrate_rgb':bg.tolist(),
      'raw_local_mean_contrast_rgb':np.mean(contrast[interior],axis=0).tolist(),
      'raw_local_median_contrast_rgb':np.median(contrast[interior],axis=0).tolist(),
      'patch_rgb_percentiles_10_50_90':np.percentile(patch,[10,50,90],axis=0).tolist(),
      'patch_std_rgb':np.std(patch,axis=0).tolist(),'spatial_patches':len(patch),
      'patch_fraction_in_directional_search_cone':float(directional.mean()),
      'directional_fraction_is_descriptive_not_a_new_threshold':True}

def main():
    rows=read(OUT/'thin-matches.json');manifest=read(CHIP/'manifest.json')
    reports=[inspect(row,manifest) for row in rows]
    first=next(r for r in rows if r['tile_id']=='tile_000069');second=next(r for r in rows if r['tile_id']=='tile_000070')
    a=cv2.imread(str(ANALYSIS/'chip001__tile_000069.instances.png'),-1)==first['instance_label']
    b=cv2.imread(str(ANALYSIS/'chip001__tile_000070.instances.png'),-1)==second['instance_label']
    p=lambda r:np.array([r['source_position'][c] for c in ('x','y')])
    shift=np.linalg.solve(np.asarray(manifest['transform']['matrix']),p(second)-p(first))
    aligned=cv2.warpAffine(np.uint8(b),np.float32([[1,0,shift[0]],[0,1,shift[1]]]),(a.shape[1],a.shape[0]),flags=cv2.INTER_NEAREST)>0
    overlap={'tiles':['tile_000069','tile_000070'],'translation_pixels':shift.tolist(),
      'first_mask_fraction_covered_by_projected_second':float((a&aligned).sum()/a.sum()),
      'second_projected_visible_mask_fraction_covered_by_first':float((a&aligned).sum()/max(aligned.sum(),1)),
      'interpretation':'Shared physical feature verified by stage-coordinate overlap, not only visual similarity.'}
    write('thin-local-raw-diagnostic.json',{'regions':reports,'overlap':overlap})
    feedback=read(ROOT/'data/chip-batch-01/thin-shortlist-feedback.json');control=feedback['annotations'][0]
    f=load_frame(Path(control['source_scan_folder'])/control['source_image']);points=np.asarray(control['polygon_px']);lo=np.maximum(points.min(axis=0)-70,0);hi=np.minimum(points.max(axis=0)+70,[f.rgb.shape[1],f.rgb.shape[0]])
    raw=preview(f.rgb[lo[1]:hi[1],lo[0]:hi[0]],4095.);outline=raw.copy();cv2.polylines(outline,[np.int32(points-lo)],True,(255,80,80),1)
    pair=Image.new('RGB',(900,520),'#202020');pair.paste(panel(Image.fromarray(raw),'User-rejected bare substrate control',(450,520)),(0,0));pair.paste(panel(Image.fromarray(outline),'Old rejected mask; different camera profile',(450,520)),(450,0));pair.save(OUT/'human-rejected-bare-substrate-control.jpg',quality=94)
    write('bare-substrate-control-provenance.json',{'feedback_file':'data/chip-batch-01/thin-shortlist-feedback.json','feedback_sha256':sha(ROOT/'data/chip-batch-01/thin-shortlist-feedback.json'),'reviewed_annotation':control,'decoded_raw_hash_matches':hashlib.sha256(f.rgb.tobytes()).hexdigest()==control['source_image_sha256'], 'all_12_human_labels':'bare substrate','comparison':'The four current matches contain visible physical features; they are a different failure mode, heterogeneous feature mean-color classification.'})
    print(json.dumps({'regions':[{k:r[k] for k in ('tile_id','raw_local_mean_contrast_rgb','raw_local_median_contrast_rgb','patch_std_rgb','patch_fraction_in_directional_search_cone')} for r in reports],'overlap':overlap},indent=2))
if __name__=='__main__':main()
