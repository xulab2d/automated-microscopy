"""Reprocessable two-pass color segmentation, with raw-image provenance and rejects."""
from dataclasses import asdict
from pathlib import Path
import hashlib, json, time
import cv2
import numpy as np
from .colors import AnalysisOptions, ColorGroups, prepare_contrast, published_shape_score, mote2_thin_screen, regional_contrast
from .imaging import load_frame, preview
from .model import CameraTransform, AcquisitionError, atomic_json
from .scan import deduplicate, export_csv

def tile_inputs(folder):
    folder=Path(folder)
    manifests=[folder/'manifest.json'] if (folder/'manifest.json').exists() else sorted(folder.glob('*/manifest.json'))
    result=[]
    for path in manifests:
        manifest=json.loads(path.read_text(encoding='utf-8'));transform=CameraTransform(**manifest['transform'])
        for name in sorted(manifest['completed']):
            source=path.parent/'tiles'/(name+'.tiff')
            if not source.exists():raise AcquisitionError(f'Missing raw tile: {source}')
            chip=manifest['plan'].get('chip_id')
            result.append({'path':source,'manifest':manifest,'transform':transform,'tile_id':name,
                           'chip_id':chip,'key':f'{chip}/{name}' if chip else name})
    if not result:raise AcquisitionError('No completed raw tiles in this scan.')
    return result

def tile_region(item,frame):
    polygon=item['manifest']['plan'].get('polygon_um')
    if polygon is None:return None
    h,w=frame.rgb.shape[:2]
    p=(np.asarray(polygon)-[frame.position.x,frame.position.y])@np.linalg.inv(item['transform'].matrix).T+[(w-1)/2,(h-1)/2]
    region=np.zeros((h,w),np.uint8);cv2.fillPoly(region,[np.int32(np.round(p))],1)
    return region


def focus_provenance(item,frame):
    """Verify recorded local focus or an interpolated, measured anchor surface.

    A surface acquisition is not local autofocus. Its saved anchors, residuals,
    hull containment, recorded prediction and actual capture Z must agree.
    """
    completed=item['manifest']['completed'][item['tile_id']]
    focus=completed.get('focus')
    if focus:
        return {**focus,'method':'local_autofocus','thin_flake_focus_validated':False},not bool(focus.get('success'))
    status={'status':'no_autofocus_at_this_tile','method':completed.get('focus_method','previous_height'),
            'thin_flake_focus_validated':False}
    if completed.get('focus_method') in ('measured_surface_interpolation','bounded_measured_plane_extrapolation'):
        try:
            from .full_chip import FullChipOptions,bounded_plane_prediction
            saved=completed['full_chip_focus']
            replay=bounded_plane_prediction(saved['anchor_points_um'],frame.position.x,frame.position.y,
                FullChipOptions(**item['manifest']['full_chip_options']))
            if replay['method']!=completed['focus_method']:raise ValueError('Recorded plane method does not match the saved anchors and capture location.')
            predicted=float(completed['focus_prediction_um'])
            if not np.isfinite(predicted) or abs(replay['predicted_z_um']-predicted)>.01 or abs(float(saved['predicted_z_um'])-predicted)>.01:
                raise ValueError('Recorded focus prediction does not match the per-tile anchor replay.')
            tolerance=float(saved['capture_z_target_tolerance_um'])
            if not np.isfinite(tolerance) or tolerance<=0:raise ValueError('Invalid recorded controller Z tolerance.')
            error=abs(frame.position.z-predicted)
            if error>tolerance:raise ValueError('Capture Z differs from the replayed plane beyond the recorded controller tolerance.')
            return {**replay,'status':'accepted_bounded_plane_replay','capture_z_target_tolerance_um':tolerance,
                'capture_z_error_um':error,'local_focus_verified':False,'focus_verified':False,
                'local_autofocus':False,'thin_flake_focus_validated':False},False
        except (ValueError,KeyError,TypeError,AcquisitionError) as exc:
            return {**status,'status':'plane_not_verified','reason':str(exc)},True
    if completed.get('focus_method')!='measured_surface':return status,True
    path=item['path'].parent.parent/'focus-surface'/'focus-surface.json'
    try:
        from .calibration import FocusMap
        surface=json.loads(path.read_text(encoding='utf-8'))
        if surface.get('state')!='complete' or surface.get('mode')!='surface':raise ValueError('Surface acquisition was not completed.')
        points=np.asarray(surface['verified_points_um'],float)
        limit=float(surface['max_residual_um'])
        if not np.isfinite(limit) or limit<=0:raise ValueError('Invalid surface residual limit.')
        fitted=FocusMap(points,max_residual_um=limit)
        design=np.column_stack([points[:,:2]-fitted.origin,np.ones(len(points))])
        residual=float(np.max(np.abs(design@fitted.coeff-points[:,2])))
        if len(fitted.points)!=len(points) or residual>limit:raise ValueError('All verified anchors do not agree with the saved surface tolerance.')
        predicted=fitted.predict(frame.position.x,frame.position.y)
        recorded=float(completed['focus_prediction_um'])
        if not np.isfinite(recorded) or abs(predicted-recorded)>.01:raise ValueError('Saved focus prediction does not match this surface.')
        z_error=abs(frame.position.z-recorded)
        if z_error>.2:raise ValueError('Captured Z differs from the surface prediction by more than 0.2 um.')
        return {'status':'verified_surface_interpolation','method':'measured_surface','success':True,
                'surface_file':str(path),'verified_anchor_count':len(points),'max_residual_um':residual,
                'allowed_residual_um':limit,'prediction_um':recorded,'capture_z_error_um':z_error,
                'inside_verified_hull':True,'local_autofocus':False,'thin_flake_focus_validated':False},False
    except (OSError,ValueError,KeyError,TypeError,AcquisitionError) as exc:
        return {**status,'status':'surface_not_verified','reason':str(exc)},True

def _boundary_evidence(local, contrast, pixel_size_um, noise_rgb, options):
    """Compare colors across measured contour normals, in physical units.

    Thresholding a smooth illumination hump can make a very solid polygon. It
    must also have a spatially localized color step; shape alone is insufficient.
    """
    binary=np.uint8(local)
    inside=cv2.distanceTransform(binary,cv2.DIST_L2,5)
    outside=cv2.distanceTransform(1-binary,cv2.DIST_L2,5)
    signed=inside-outside
    gx=cv2.Sobel(signed,cv2.CV_32F,1,0,ksize=3)
    gy=cv2.Sobel(signed,cv2.CV_32F,0,1,ksize=3)
    contours,_=cv2.findContours(binary,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
    contour=max(contours,key=cv2.contourArea)
    points=contour[:,0,:].astype(float)
    points=points[::max(1,len(points)//1500)]
    x=points[:,0].astype(int);y=points[:,1].astype(int)
    normals=np.stack([gx[y,x],gy[y,x]],axis=1)
    normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-6)
    step=options.boundary_probe_um/np.asarray(pixel_size_um)
    a=points+normals*step;b=points-normals*step
    def sample(points):
        return cv2.remap(np.float32(contrast),np.float32(points[:,0,None]),
                         np.float32(points[:,1,None]),cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REFLECT_101)[:,0,:]
    difference=sample(a)-sample(b)
    magnitude=np.linalg.norm(difference,axis=1)
    snr=np.linalg.norm(difference/np.maximum(noise_rgb,1e-6),axis=1)
    near=sample(points+.35*normals*step)-sample(points-.35*normals*step)
    near_projected=np.sum(near*difference,axis=1)/np.maximum(magnitude,1e-9)
    # A smooth color slope grows linearly with probe separation (ratio ~.35).
    # A resolved interface concentrates more of its transition near the edge.
    localized=near_projected>=.4*magnitude
    supported=(magnitude>=options.min_contrast)&(snr>=5)&localized
    return float(np.mean(supported)),float(np.median(magnitude)),inside


def _facet_fraction(contour,pixel_size_um):
    """Fraction of perimeter carried by long straight segments.

    This is an interpretable proposal filter, not a trained crystal classifier.
    No preferred crystal orientation is imposed.
    """
    from scipy.ndimage import gaussian_filter1d
    physical=np.float32(contour)*np.float32(pixel_size_um)
    # Smooth sub-resolution edge jitter along the contour before fitting facets;
    # a jagged one-pixel threshold perimeter must not erase a real straight edge.
    physical=gaussian_filter1d(physical,1/min(pixel_size_um),axis=0,mode='wrap')
    tolerance=max(.5,min(pixel_size_um))
    polygon=cv2.approxPolyDP(physical,tolerance,True)[:,0,:]
    # A crystalline polygon concentrates perimeter into a few edge directions.
    # Measure directions on the smoothed contour, rather than demanding that a
    # single RDP segment occupy an arbitrary fraction of the entire perimeter.
    points=physical[:,0,:]
    delta=np.roll(points,-2,axis=0)-np.roll(points,2,axis=0)
    weights=np.linalg.norm(np.roll(points,-1,axis=0)-points,axis=1)
    angles=np.mod(np.arctan2(delta[:,1],delta[:,0]),np.pi)
    hist,_=np.histogram(angles,bins=36,range=(0,np.pi),weights=weights)
    total=float(hist.sum());remaining=hist.copy();supported=0.
    for _ in range(3):
        window=np.roll(remaining,1)+remaining+np.roll(remaining,-1)
        k=int(np.argmax(window));ids=[(k-1)%36,k,(k+1)%36]
        supported+=float(remaining[ids].sum());remaining[ids]=0
    return float(supported/max(total,1e-6)),len(polygon)


def _refine_objects(frame,prepared,options,return_labels=False):
    """Graph-cut refinement with automatically chosen spatial foreground seeds.

    Thresholded contrast is a proposal envelope. GrabCut learns local RGB
    foreground/background models to put the contour on the object, not its halo.
    """
    initial=np.uint8(prepared['foreground'])
    n,labels,stats,_=cv2.connectedComponentsWithStats(initial,8)
    result=np.zeros(initial.shape,np.int32);height,width=initial.shape;next_label=1
    padding=max(8,int(np.ceil(4/min(frame.pixel_size_um))))
    for j in range(1,n):
        x,y,w,h,count=map(int,stats[j])
        if count*np.prod(frame.pixel_size_um)<options.min_detection_area_um2:continue
        x0=max(0,x-padding);x1=min(width,x+w+padding)
        y0=max(0,y-padding);y1=min(height,y+h+padding)
        local=np.uint8(labels[y0:y1,x0:x1]==j)
        dist=cv2.distanceTransform(local,cv2.DIST_L2,5)
        strength=np.linalg.norm(prepared['smooth'][y0:y1,x0:x1],axis=2)
        sy,sx=np.unravel_index(np.argmax(dist*strength),local.shape)
        rgb=frame.rgb[y0:y1,x0:x1].astype(np.float32)
        scale=max(float(np.percentile(rgb,99.8)),1)
        image=np.uint8(np.clip(rgb/scale,0,1)*255)
        mask=np.full(local.shape,cv2.GC_BGD,np.uint8)
        mask[y-y0:y-y0+h,x-x0:x-x0+w]=cv2.GC_PR_BGD
        mask[local>0]=cv2.GC_PR_FGD
        mask[sy,sx]=cv2.GC_FGD
        try:
            # Keep a tile/envelope reproducible when new smaller envelopes are
            # retained earlier in the loop. GrabCut initializes its color GMM
            # from OpenCV's RNG; otherwise adding a component changes later masks.
            cv2.setRNGSeed((x*73856093 ^ y*19349663 ^ w*83492791 ^ h)&0x7fffffff)
            cv2.grabCut(image,mask,None,np.zeros((1,65),float),np.zeros((1,65),float),4,cv2.GC_INIT_WITH_MASK)
        except cv2.error:continue
        foreground=np.uint8((mask==cv2.GC_FGD)|(mask==cv2.GC_PR_FGD))
        # An envelope can contain several colors/objects. Keeping only the
        # strongest seed's component deleted otherwise valid disconnected
        # pieces. Keep every supported component separately, preserving holes.
        parts,pieces,part_stats,_=cv2.connectedComponentsWithStats(foreground,8)
        destination=result[y0:y1,x0:x1]
        for piece_id in range(1,parts):
            bx,by,bw,bh,pixels=map(int,part_stats[piece_id])
            if pixels*np.prod(frame.pixel_size_um)<options.min_detection_area_um2:continue
            piece=pieces[by:by+bh,bx:bx+bw]==piece_id
            if not np.any(piece&(local[by:by+bh,bx:bx+bw]>0)):continue
            dest=destination[by:by+bh,bx:bx+bw]
            available=np.uint8(piece&(dest==0))
            available_count,available_labels,available_stats,_=cv2.connectedComponentsWithStats(available,8)
            for available_id in range(1,available_count):
                if available_stats[available_id,cv2.CC_STAT_AREA]*np.prod(frame.pixel_size_um)<options.min_detection_area_um2:continue
                dest[available_labels==available_id]=next_label;next_label+=1
    return result if return_labels else np.uint8(result>0)


def extract_regions(frame,prepared,groups,options,transform=None,coherent=False,region_labels=None,region_evidence=None):
    # Foreground connectivity owns the outer object. A fitted color component is
    # only a descriptor and cannot create hundreds of arbitrary small masks.
    evidence={}
    if coherent:
        from .coherent_regions import coherent_region_labels
        if region_labels is None:components,evidence=coherent_region_labels(frame,prepared,options)
        else:components,evidence=region_labels,region_evidence or {}
    else:
        components=_refine_objects(frame,prepared,options,return_labels=True)
    from scipy.ndimage import find_objects,center_of_mass
    n=int(components.max())+1;stats=np.zeros((n,5),int);centres=np.zeros((n,2),float)
    if n>65536:raise AcquisitionError('More than 65535 source instances; use a larger detection noise floor.')
    counts=np.bincount(components.ravel(),minlength=n)
    for j,bounds in enumerate(find_objects(components),1):
        if bounds is None:continue
        ys,xs=bounds;stats[j]=[xs.start,ys.start,xs.stop-xs.start,ys.stop-ys.start,counts[j]]
        local=components[ys,xs]==j;cy,cx=center_of_mass(local)
        centres[j]=[xs.start+cx,ys.start+cy]
    semantic=np.zeros(components.shape,np.uint16)
    rows=[];pxarea=float(np.prod(frame.pixel_size_um));height,width=components.shape
    padding=max(4,int(np.ceil(8/min(frame.pixel_size_um))))
    for index in range(1,n):
        x,y,w,h,count=map(int,stats[index]);area=float(count*pxarea)
        if area<options.min_detection_area_um2:continue
        x0=max(x-padding,0);y0=max(y-padding,0)
        x1=min(x+w+padding,width);y1=min(y+h+padding,height)
        local=np.uint8(components[y0:y1,x0:x1]==index)
        contours,_=cv2.findContours(local,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
        contour=max(contours,key=cv2.contourArea)
        contour_area=max(cv2.contourArea(contour),1)
        solidity=float(contour_area/max(cv2.contourArea(cv2.convexHull(contour)),1))
        perimeter=float(cv2.arcLength(contour,True)/np.sqrt(contour_area))
        filled=np.zeros_like(local);cv2.drawContours(filled,[contour],-1,1,cv2.FILLED)
        coverage=float(local.sum()/max(filled.sum(),1))
        roi=prepared['smooth'][y0:y1,x0:x1]
        support,edge_step,dist=_boundary_evidence(local,roi,frame.pixel_size_um,
                            prepared.get('noise_rgb',np.full(3,.002)),options)
        usable_width=float(2*dist.max()*min(frame.pixel_size_um))
        facet_fraction,facet_vertices=_facet_fraction(contour,frame.pixel_size_um)
        inside=(dist>=max(1,options.boundary_probe_um/min(frame.pixel_size_um)))
        if inside.sum()<6:inside=local.astype(bool)
        ring_outer=cv2.dilate(local,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*int(np.ceil(7/min(frame.pixel_size_um)))+1,)*2))
        ring_inner=cv2.dilate(local,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*int(np.ceil(2/min(frame.pixel_size_um)))+1,)*2))
        background_ring=ring_outer.astype(bool)&~ring_inner.astype(bool)&~prepared['foreground'][y0:y1,x0:x1]&prepared['valid'][y0:y1,x0:x1]
        regional=regional_contrast(frame.rgb[y0:y1,x0:x1],inside,background_ring,frame.pixel_size_um)
        colors=prepared['contrast'][y0:y1,x0:x1][inside]
        mean=np.median(colors,axis=0)
        color_labels,probability,_=groups.predict(roi[local.astype(bool)])
        group_index=int(np.bincount(color_labels[color_labels>=0]).argmax()) if np.any(color_labels>=0) else -1
        group_id=groups.groups[group_index]['id'] if group_index>=0 else 'unassigned'
        purity=float(np.mean(probability)) if len(probability) else 0
        # Terraces remain attached to the parent object. They are descriptive
        # regions and do not establish a layer count or material identity.
        # Fit local optical domains inside this object. A scene-wide mixture can
        # merge a small thin terrace with tape elsewhere in the image.
        sample=roi[inside]
        sample=sample[::max(1,len(sample)//1500)]
        # V2 already represents one optical color domain per instance. Fitting
        # it again would create arbitrary subpatches and obscure its exact area.
        local_model=ColorGroups({'groups':[]}) if coherent or area<options.min_area_um2 else ColorGroups.fit(sample,min(4,options.max_components))
        local_groups=np.full(local.shape,-1,np.int16)
        if not coherent:local_groups[local.astype(bool)]=local_model.predict(roi[local.astype(bool)])[0]
        highpass=roi-cv2.GaussianBlur(roi,(0,0),max(1,.5/min(frame.pixel_size_um)))
        noise=prepared.get('noise_rgb',np.full(3,.002))
        texture_limit=max(options.max_texture,2*float(np.linalg.norm(noise)))
        terraces=[];residuals=[]
        for j,group in enumerate(local_model.groups):
            subset=inside&(local_groups==j)
            if subset.sum()<6:continue
            values=prepared['contrast'][y0:y1,x0:x1][subset]
            centre=np.median(values,axis=0)
            residuals.extend(np.linalg.norm(values-centre,axis=1).tolist())
            domain=np.uint8((local_groups==j)&local.astype(bool))
            domain=cv2.morphologyEx(domain,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
            nc,lc,sc,_=cv2.connectedComponentsWithStats(domain,8)
            for k in range(1,nc):
                domain_area=float(sc[k,cv2.CC_STAT_AREA]*pxarea)
                if domain_area<options.min_area_um2:continue
                dmask=np.uint8(lc==k)
                dc,_=cv2.findContours(dmask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)
                dc=max(dc,key=cv2.contourArea)
                dp=cv2.approxPolyDP(dc,.7,True)[:,0,:]+[x0,y0]
                darea=max(cv2.contourArea(dc),1)
                dsolidity=float(darea/max(cv2.contourArea(cv2.convexHull(dc)),1))
                dperimeter=float(cv2.arcLength(dc,True)/np.sqrt(darea))
                dsupport,dstep,ddist=_boundary_evidence(dmask,roi,frame.pixel_size_um,noise,options)
                dwidth=float(2*ddist.max()*min(frame.pixel_size_um))
                dfacets,_=_facet_fraction(dc,frame.pixel_size_um)
                dinside=ddist>=max(1,options.boundary_probe_um/min(frame.pixel_size_um))
                if dinside.sum()<6:dinside=dmask.astype(bool)
                dvalues=prepared['contrast'][y0:y1,x0:x1][dinside]
                dmean=np.median(dvalues,axis=0)
                dregional=regional_contrast(frame.rgb[y0:y1,x0:x1],dinside,background_ring,frame.pixel_size_um)
                duniformity=float(np.median(np.linalg.norm(dvalues-dmean,axis=1)))
                dtexture=float(np.percentile(np.linalg.norm(highpass[dinside],axis=1),75))
                dclip=bool((x0==0 and dmask[:,0].any()) or (y0==0 and dmask[0,:].any()) or
                           (x1==width and dmask[:,-1].any()) or (y1==height and dmask[-1,:].any()))
                dreasons=[]
                if domain_area>options.max_area_um2:dreasons.append('oversized')
                if dwidth<options.min_width_um:dreasons.append('insufficient_usable_width')
                dshape=published_shape_score(dperimeter,dsolidity)
                if dshape<options.min_shape_score:dreasons.append('published_shape_discriminator')
                if dsupport<options.min_boundary_support:dreasons.append('unsupported_or_diffuse_boundary')
                if duniformity>max(options.max_uniformity,2*float(np.linalg.norm(noise))):dreasons.append('nonuniform_color')
                if dtexture>texture_limit:dreasons.append('textured_interior')
                if dclip:dreasons.append('clipped_or_chip_edge')
                descriptor=groups.predict(dmean[None])[0][0]
                terraces.append({'color_group':groups.groups[descriptor]['id'] if descriptor>=0 else 'unassigned',
                                 'area_um2':domain_area,'contrast_rgb':dregional['contrast_rgb'] if dregional else dmean.tolist(),'polygon_px':dp.tolist(),
                                 'regional_measurement':dregional,
                                 'usable_width_um':dwidth,'boundary_support':dsupport,'facet_fraction':dfacets,
                                 'published_shape_score':dshape,
                                 'uniformity':duniformity,'interior_texture':dtexture,
                                 'disposition':'rejected' if dreasons else 'candidate','filter_reasons':dreasons})
        uniformity=float(np.median(residuals)) if residuals else float(np.median(np.linalg.norm(colors-mean,axis=1)))
        highpass=roi-cv2.GaussianBlur(roi,(0,0),max(1,.5/min(frame.pixel_size_um)))
        texture=float(np.percentile(np.linalg.norm(highpass[inside],axis=1),75))
        px=contour[:,0,:]+[x0,y0]
        border=bool(x==0 or y==0 or x+w>=width or y+h>=height)
        border|=bool(np.any(cv2.dilate(local,np.ones((5,5),np.uint8)).astype(bool)&~prepared['valid'][y0:y1,x0:x1]))
        reasons=[]
        if area<options.min_area_um2:reasons.append('below_useful_area')
        if area>options.max_area_um2:reasons.append('oversized')
        shape_score=published_shape_score(perimeter,solidity)
        if shape_score<options.min_shape_score:reasons.append('ragged_or_filamentary')
        if usable_width<options.min_width_um:reasons.append('insufficient_usable_width')
        if support<options.min_boundary_support:reasons.append('unsupported_or_diffuse_boundary')
        if coverage<.8:reasons.append('hollow_or_fragmented_interior')
        if uniformity>options.max_uniformity:reasons.append('nonuniform_terraces')
        # Camera noise is not specimen texture; compare with the measured noise
        # floor as well as an absolute contrast threshold.
        texture_limit=max(options.max_texture,2*float(np.linalg.norm(prepared.get('noise_rgb',np.full(3,.002)))))
        if texture>texture_limit:reasons.append('textured_interior')
        if border:reasons.append('clipped_or_chip_edge')
        parent_reasons=list(reasons)
        useful_terraces=[t for t in terraces if t['disposition']=='candidate']
        if useful_terraces:reasons=[]
        quality=float(np.clip(shape_score*support,0,1))
        poly=cv2.approxPolyDP(np.int32(px).reshape(-1,1,2),.7,True)[:,0,:].tolist()
        centre=centres[index].tolist()
        row={'centroid_px':centre,'polygon_px':poly,'bbox_px':[x,y,w,h],
             'area_um2':area,'usable_width_um':usable_width,'contrast_rgb':regional['contrast_rgb'] if regional else mean.tolist(),
             'regional_measurement':regional,
             'contrast_norm':float(np.linalg.norm(mean)),'uniformity':uniformity,
             'solidity':solidity,'perimeter_ratio':perimeter,'touches_border':border,
             'boundary_support':support,'boundary_step':edge_step,'facet_fraction':facet_fraction,
             'published_shape_score':shape_score,
             'facet_vertices':facet_vertices,'interior_coverage':coverage,'interior_texture':texture,
             'terraces':terraces,'parent_filter_reasons':parent_reasons,
             'useful_area_um2':max([t['area_um2'] for t in useful_terraces],default=area if not reasons else 0),
             'color_group':group_id,'color_posterior':purity,'quality_score':quality,
             'filter_reasons':reasons,'disposition':'rejected' if reasons else 'candidate',
             'label':'unclassified','distance':None,'stage_xy_um':None,'stage_polygon_um':None}
        boundaries,hierarchy=cv2.findContours(local,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
        row['holes_px']=[(cv2.approxPolyDP(c,.5,True)[:,0,:]+[x0,y0]).tolist()
            for j,c in enumerate(boundaries) if hierarchy[0,j,3]>=0]
        row['mask_area_pixels']=count
        row['instance_label']=index
        row.update(evidence.get(index,{}))
        if transform:
            row['stage_xy_um']=transform.stage_xy(centre,frame).tolist()
            row['stage_polygon_um']=[transform.stage_xy(p,frame).tolist() for p in poly]
        semantic[y0:y1,x0:x1][local.astype(bool)]=index
        rows.append(row)
    return rows,semantic


def empirical_proposals(rows,options):
    """Keep useful optical regions for review, recording strict evidence flags.

    This deliberately trades precision for an inspectable shortlist. A diffuse
    patch can remain a proposal; neither contrast nor GrabCut proves a crystal.
    The strict mode's decisions are preserved to make that tradeoff auditable.
    """
    def size_reasons(region):
        reasons=[]
        if region['area_um2']<options.min_area_um2:reasons.append('below_useful_area')
        if region['area_um2']>options.max_area_um2:reasons.append('oversized')
        if region['usable_width_um']<options.min_width_um:reasons.append('insufficient_usable_width')
        return reasons
    for row in rows:
        row['strict_disposition']=row['disposition']
        row['strict_filter_reasons']=list(row['filter_reasons'])
        row['evidence_warnings']=list(row.get('parent_filter_reasons',row['filter_reasons']))
        useful=[]
        for terrace in row.get('terraces',[]):
            terrace['strict_disposition']=terrace['disposition']
            terrace['evidence_warnings']=list(terrace['filter_reasons'])
            terrace['filter_reasons']=size_reasons(terrace)
            terrace['disposition']='rejected' if terrace['filter_reasons'] else 'candidate'
            if terrace['disposition']=='candidate':useful.append(terrace)
        reasons=size_reasons(row)
        row['parent_size_filter_reasons']=list(reasons)
        if useful:reasons=[]
        row.update(filter_reasons=reasons,disposition='rejected' if reasons else 'candidate',
                   useful_area_um2=max([t['area_um2'] for t in useful],default=row['area_um2'] if not reasons else 0),
                   proposal_mode='empirical_optical_region',
                   extraction_method=('Connected local contrast-color domain; substrate and distinct color regions remain separate; physical size screening only' if row.get('segmentation_method')=='local_contrast_connected_domain' else 'Contrast foreground with automatic GrabCut boundary; physical size screening only'),
                   quality_score_meaning='Strict shape/boundary evidence descriptor; not an empirical acceptance test or flake probability.',
                   validation_status='unvalidated_optical_proposal',layer_number=None)
        if row.get('segmentation_method')=='whole_field_color_population_connected_domain':
            row['extraction_method']='Whole-field optical color model with connected domains and exact holes; physical catalog size screening only'
    return rows


def assign_mote2_screen(row):
    """Keep color compatibility distinct from an evidence-qualified shortlist.

    Empirical mode relaxes object rejection to show all optical proposals. It
    must not erase the stored strict quality failures when ranking thin targets.
    No new quality threshold is introduced here.
    """
    choices=[]
    parent_reasons=list(row.get('parent_filter_reasons',row.get('strict_filter_reasons',row.get('filter_reasons',[]))))
    parent_reasons+=list(row.get('parent_size_filter_reasons',[]))
    parent_evidence_available=any(key in row for key in ('parent_filter_reasons','strict_filter_reasons','filter_reasons'))
    parent_passed=parent_evidence_available and not parent_reasons and row.get('strict_disposition',row.get('disposition'))=='candidate'
    choices.append((mote2_thin_screen(row['contrast_rgb']),row['area_um2'],row['contrast_rgb'],'parent',parent_passed,parent_reasons))
    for i,terrace in enumerate(row.get('terraces',[])):
        compatibility=mote2_thin_screen(terrace['contrast_rgb'])
        reasons=list(terrace.get('evidence_warnings',terrace.get('filter_reasons',[])))
        eligible=terrace.get('strict_disposition',terrace.get('disposition'))=='candidate' and not reasons
        terrace['color_compatibility']=dict(compatibility)
        terrace['thin_screen']=dict(compatibility)
        terrace['thin_screen']['object_evidence_passed']=eligible
        if compatibility['score'] and not eligible:
            terrace['thin_screen'].update(status='color_match_only',score=0.,compatibility_score=compatibility['score'],object_quality_reasons=reasons)
        choices.append((compatibility,terrace['area_um2'],terrace['contrast_rgb'],f'terrace:{i}',eligible,reasons))
    color=max(choices,key=lambda item:item[0]['score'])
    row['color_compatibility']=dict(color[0])
    row['color_compatibility_area_um2']=color[1] if color[0]['score'] else 0.
    row['color_compatibility_contrast_rgb']=color[2]
    row['color_compatibility_region']=color[3]
    qualified=[item for item in choices if item[4] and item[0]['score']>0]
    selected=max(qualified,key=lambda item:item[0]['score']) if qualified else color
    screen,area,contrast,region,eligible,reasons=selected
    row['thin_screen']=dict(screen)
    row['thin_screen']['object_evidence_passed']=eligible
    row['thin_screen']['object_quality_reasons']=reasons
    if screen['score'] and not qualified:
        row['thin_screen'].update(status='color_match_only',score=0.,compatibility_score=screen['score'],
            qualification='Color compatibility only; existing object-quality evidence failed. Not a credible thin-flake shortlist entry.')
    row['thin_screen_area_um2']=area if qualified else 0.
    row['thin_screen_contrast_rgb']=contrast
    row['thin_screen_region']=region
    if row.get('disposition')=='rejected':row['priority']='rejected'
    else:row['priority']='thin_screen' if qualified else 'color_match_only' if screen['score'] else 'review_required'
    return row


def analyze_scan(folder,options=None,output=None,callback=None,model=None,check_cancel=None):
    """Analyze saved tiles with learned masks, or the experimental color backend.

    Analysis never modifies raw tiles or acquisition checkpoints. A separate output
    folder supports direct comparisons between versions without acquiring again.
    """
    from .storage import managed_output_path
    folder=Path(folder).resolve();out=managed_output_path(output if output else folder/'analysis')
    out.mkdir(parents=True,exist_ok=True)
    values=asdict(options) if isinstance(options,AnalysisOptions) else dict(options or {})
    if values.get('backend')=='thin_regions':
        from .thin_analysis import analyze_thin_scan
        return analyze_thin_scan(folder,values,out,callback,check_cancel)
    backend=values.pop('backend','auto');learned=values.pop('learned',{});reference=values.pop('reference',{})
    material_profile=values.pop('material_profile',None)
    if isinstance(material_profile,(str,Path)):
        material_profile=json.loads(Path(material_profile).read_text(encoding='utf-8'))
    if material_profile and backend not in ('empirical','empirical_v2','classical','global_color_domains'):
        raise ValueError('Material reference selection requires an explicit saved-mask analysis backend.')
    if backend not in ('auto','learned','classical','empirical','empirical_v2','bulk_reference','global_color_domains'):raise ValueError('Unknown analysis backend.')
    empirical=backend in ('empirical','empirical_v2','global_color_domains')
    bulk_reference=backend=='bulk_reference'
    global_domains=backend=='global_color_domains'
    instance_backend=True
    options=AnalysisOptions(**values)
    from .learned_analysis import learned_runtime_available, analyze_learned_scan
    simulated=backend=='auto' and all(item['manifest'].get('source')=='SIMULATION' for item in tile_inputs(folder))
    if backend=='learned' or (backend=='auto' and not simulated and (learned.get('predictions') or learned_runtime_available())):
        return analyze_learned_scan(folder,out,options,learned,callback,model,check_cancel)
    tiles=tile_inputs(folder);rng=np.random.default_rng(42);samples=[];cloud=[];tile_data=[]
    # A shared color model cannot combine different optical acquisition settings.
    contexts=[]
    for item in tiles:
        context=item['manifest'].get('context',{})
        contexts.append(json.dumps({k:context.get(k) for k in ('sample_id','material','substrate','oxide_nm','objective','camera','exposure_ms','illumination','illumination_percent','white_balance')},sort_keys=True))
    if len(set(contexts))>1:raise AcquisitionError('Tiles have different material/optical contexts. Analyze each fixed acquisition profile separately.')
    if material_profile:
        from .material_selection import optical_context,classify_frame_regions
        if any(optical_context(item['manifest'])!=material_profile['optical_context'] for item in tiles):
            raise AcquisitionError('Material reference profile does not match this optical/sample context.')
    signature=hashlib.sha256()
    algorithm='whole-field-global-color-domains-v1' if global_domains else 'user-bulk-contrast-reference-v1' if bulk_reference else 'local-contrast-connected-domains-v2' if backend=='empirical_v2' else 'empirical-all-supported-components-deterministic-v2' if empirical else 'object-evidence-all-supported-components-deterministic-v3'
    signature.update(json.dumps({'algorithm':algorithm,'options':asdict(options),
                                 'model':None if model is None else model.data,
                                 'reference':reference if bulk_reference else None},sort_keys=True).encode())
    def progress(phase,i,total):
        if check_cancel:check_cancel()
        if callback:callback(phase,{'index':i,'total':total})
    for i,item in enumerate(tiles):
        f=load_frame(item['path']);item['transform'].validate_frame(f)
        digest=hashlib.sha256(f.rgb.tobytes()).hexdigest();signature.update((item['key']+digest).encode())
        h,w=f.rgb.shape[:2]
        record={'key':item['key'],'tile_id':item['tile_id'],'chip_id':item['chip_id'],
                'source':str(item['path'].relative_to(folder).as_posix()),'position':asdict(f.position),
                'shape':[h,w],'pixel_size_um':list(f.pixel_size_um),'sha256':digest,
                'footprint_um':[item['transform'].stage_xy(p,f).tolist() for p in [[0,0],[w-1,0],[w-1,h-1],[0,h-1]]],
                'acquired_at':f.acquired_at}
        record['focus_status'],record['focus_limited']=focus_provenance(item,f)
        try:
            prepared=prepare_contrast(f,options,tile_region(item,f))
            population=prepared['smooth'][prepared['valid'] if global_domains else prepared['foreground']]
            if len(population):
                chosen=population[rng.choice(len(population),min(len(population),options.sample_per_tile),replace=False)]
                samples.append(chosen)
                cloud.extend([{'rgb':v.tolist(),'tile':item['key'],'population':'whole_field' if global_domains else 'foreground'} for v in chosen[:400]])
            bg=prepared['smooth'][prepared['valid']&~prepared['foreground']]
            if len(bg):
                cloud.extend([{'rgb':v.tolist(),'tile':item['key'],'population':'substrate'} for v in bg[rng.choice(len(bg),min(150,len(bg)),replace=False)]])
            record.update(substrate_fraction=float(prepared['valid'].mean()),foreground_fraction=prepared['foreground_fraction'])
        except AcquisitionError as exc:
            record['analysis_error']=str(exc)
        tile_data.append(record);progress('Sampling color space',i+1,len(tiles))
    groups=ColorGroups({'schema':1,'groups':[],'method':'Whole-field per-tile optical populations' if global_domains else 'User-identified bulk contrast population'}) if bulk_reference or global_domains else ColorGroups.fit(np.concatenate(samples) if samples else np.empty((0,3)),options.max_components)
    atomic_json(out/'color-model.json',groups.data)
    rows=[]
    for i,(item,record) in enumerate(zip(tiles,tile_data)):
        if record.get('analysis_error'):continue
        f=load_frame(item['path']);prepared=prepare_contrast(f,options,tile_region(item,f))
        if instance_backend:
            h,w=f.rgb.shape[:2];mh=max(1,int(np.ceil(h/4)));mw=max(1,int(np.ceil(w/4)))
            contrast_map=cv2.resize(prepared['contrast'][:,:,:2],(mw,mh),interpolation=cv2.INTER_AREA)
            contrast_name=item['key'].replace('/','__')+'.contrast-rg.npz'
            np.savez_compressed(out/contrast_name,contrast_rg=np.float32(contrast_map),original_shape_hw=np.array([h,w]),
                                source_pixels_per_map_pixel=np.array([w/mw,h/mh]))
            record['contrast_rg_file']=contrast_name
            record['contrast_map_note']='Red/green raw linear contrast (fitted local substrate minus image)/substrate; 4x area average for display, not a layer calibration.'
        if bulk_reference:
            from .contrast_reference import bulk_reference_labels
            regions,evidence=bulk_reference_labels(f,prepared,options,reference.get('ranges',(-.8,-.5)))
            found,semantic=extract_regions(f,prepared,groups,options,item['transform'],coherent=True,
                                           region_labels=regions,region_evidence=evidence)
            for row in found:
                row['evidence_warnings']=row['parent_filter_reasons'];row['strict_filter_reasons']=row['filter_reasons']
                useful=row['area_um2']>=options.min_area_um2
                row.update(disposition='candidate' if useful else 'rejected',filter_reasons=[] if useful else ['below_useful_area'],label='bulk contrast reference',layer_number=None,
                    review_role='bulk_positive_control',
                    extraction_method='Whole-image RGB contrast classification; connected optical-mixture boundary',
                    validation_status='user_identified_bulk_population; mask and recall require review',
                    useful_area_um2=0,thin_screen_area_um2=0,
                    thin_screen={'status':'not_applicable_bulk_reference','score':0.,'layer_number':None})
        elif global_domains:
            from .global_domains import global_domain_labels
            regions,evidence,report=global_domain_labels(f,prepared,options)
            name=item['key'].replace('/','__')+'.global-model.json'
            atomic_json(out/name,report);record['global_color_model_file']=name
            record['segmentation_seconds']=report['elapsed_seconds']
            found,semantic=extract_regions(f,prepared,ColorGroups(report['model']),options,item['transform'],coherent=True,
                                           region_labels=regions,region_evidence=evidence)
        else:found,semantic=extract_regions(f,prepared,groups,options,item['transform'],coherent=backend=='empirical_v2')
        if empirical:found=empirical_proposals(found,options)
        if backend=='empirical_v2' or global_domains:
            for row in found:row['review_role']='faint_unvalidated'
        for row in found:
            key=item['key']+'|'+','.join(map(str,np.round(row['centroid_px'],1)))+'|'+row['color_group']
            row.update(id='f-'+hashlib.sha256(key.encode()).hexdigest()[:14],tile_id=item['tile_id'],
                tile_key=item['key'],chip_id=item['chip_id'],source_image=record['source'],
                focus_limited=record['focus_limited'],focus_status=record['focus_status'])
            if item['manifest'].get('revisit_parent_id'):
                row['parent_candidate_id']=item['manifest']['revisit_parent_id']
                identity=item['manifest']['completed'][item['tile_id']].get('registration',{})
                if identity.get('candidate_px'):
                    point=identity['candidate_px']
                    row['contains_revisited_candidate']=cv2.pointPolygonTest(np.float32(row['polygon_px']),tuple(point),False)>=0
            if model and not bulk_reference:row['label'],row['distance']=model.classify(row['contrast_rgb'],item['manifest'].get('context',{}))
            material=str(item['manifest'].get('context',{}).get('material','')).lower().replace('₂','2')
            if material=='mote2' and not bulk_reference:
                assign_mote2_screen(row)
        if material_profile:
            selections=classify_frame_regions(f,prepared['contrast'],semantic,found,material_profile,prepared['valid'])
            for row in found:
                row['material_selection']=selections.get(row['id'],{
                    'status':'uncertain','catalog_selected':False,'reason':'No matching source mask.',
                    'model_id':material_profile['model_id']})
        rows.extend(found)
        record['regions']=len(found);record['candidates']=sum(r['disposition']=='candidate' for r in found)
        mask_name=item['key'].replace('/','__')+('.instances.png' if instance_backend else '.colors.png')
        cv2.imwrite(str(out/mask_name),semantic);record['color_mask']=mask_name
        if instance_backend:record['instance_mask']=mask_name
        progress('Segmenting color domains',i+1,len(tiles))
    # Keep the best full observation; rejected fragments remain available separately.
    accepted=deduplicate([r for r in rows if r['disposition']=='candidate'])
    rejected=[r for r in rows if r['disposition']=='rejected']
    counts={g['id']:sum(r['color_group']==g['id'] for r in accepted) for g in groups.groups}
    for g in groups.groups:g['candidate_count']=counts[g['id']]
    # Low contrast is not evidence for a thin flake; only reviewed objects can
    # be treated as preferred revisit targets until a material model is tested.
    for r in accepted:
        status=r.get('thin_screen',{}).get('status','review_required')
        r['priority']='bulk_reference' if bulk_reference else ('thin_screen' if status=='thin_screen' else 'color_match_only' if status=='color_match_only' else 'review_required') if empirical else status
    for r in rejected:r['priority']='rejected'
    accepted.sort(key=lambda r:(-r.get('thin_screen',{}).get('score',0),-r.get('thin_screen_area_um2',r['area_um2']),-r['quality_score']))
    all_rows=accepted+rejected
    if instance_backend:
        catalog_ids={row['id']:row['id'] for row in accepted}
        for row in accepted:
            for observation in row.get('overlap_observations',[]):catalog_ids[observation['id']]=row['id']
        for row in rows:row['catalog_id']=catalog_ids.get(row['id'])
        atomic_json(out/'observations.json',{'schema':1,'observations':rows,'physical_unique_count':len(accepted),
            'source_folder':str(folder),'detection_min_area_um2':options.min_detection_area_um2,
            'catalog_min_area_um2':options.min_area_um2,'physical_unique_count_scope':'size-qualified catalog objects'})
    summary={'schema':2,'backend':'global_color_domains_experimental' if global_domains else 'bulk_contrast_reference' if bulk_reference else 'empirical_connected_domains_v2' if backend=='empirical_v2' else 'empirical_optical_proposals' if empirical else 'classical_experimental','dataset_id':signature.hexdigest()[:20],'source_folder':str(folder),
        'created_at':time.time(),'options':asdict(options),'tiles':tile_data,'color_model':groups.data,
        'color_cloud':cloud,'raw_regions':len(rows),'candidate_count':len(accepted),'rejected_count':len(rejected),
        'deduplicated_count':len(rows)-len(all_rows),'distinct_images':len({r['sha256'] for r in tile_data}),
        'focus_limited_tiles':sum(r['focus_limited'] for r in tile_data),
        'thin_screen_count':sum(r['priority']=='thin_screen' for r in accepted),
        'color_match_only_count':sum(r['priority']=='color_match_only' for r in accepted),
        'validation_status':'unvalidated_optical_proposals' if empirical else 'unvalidated_object_proposals',
        'warning':('Size-screened optical regions for visual review; shape, boundary and texture warnings do not veto empirical proposals. Tape, haze and bulk fragments may remain. No material identity or layer number is established.' if empirical else 'Shape and boundary filters generate review proposals. Color groups do not establish material identity or layer number. No confirmed MoTe2 examples have validated this detector.')}
    if bulk_reference:
        summary.update(validation_status='bulk_reference_recall_control',review_role='bulk_positive_control',
            observed_count=len(rows),below_catalog_size_count=sum(r['area_um2']<options.min_area_um2 for r in rows),
            reference={'axes':['red','green'],'ranges':reference.get('ranges',[[-.8,-.5],[-.8,-.5]]),
                       'minimum_detection_area_um2':options.min_detection_area_um2,'catalog_minimum_area_um2':options.min_area_um2},
            warning='User-identified bulk contrast population, searched over all image pixels. Source overlays retain all sizes and overlap views; the size-qualified catalog is separate. These are thick reference objects, not useful thin targets. No monolayer assignment is made.')
    if material_profile:
        summary['material_selection']={'model_id':material_profile['model_id'],'profile':'material-profile.json',
            'catalog_selected_count':sum(r['material_selection'].get('catalog_selected',False) for r in accepted),
            'catalog_status_counts':{status:sum(r['material_selection']['status']==status for r in accepted)
                                     for status in ('material_like','uncertain','excluded')},
            'mask_policy':'Original object boundaries retained; contiguous color support controls material selection.',
            'qualification':material_profile['qualification']}
        atomic_json(out/'material-profile.json',material_profile)
    atomic_json(out/'analysis.json',summary)
    atomic_json(out/'flakes.json',{'flakes':all_rows,'analysis_schema':2,'dataset_id':summary['dataset_id'],'source_folder':str(folder)})
    export_csv(out/'flakes.csv',all_rows)
    from .review import build_review
    build_review(out)
    if empirical or bulk_reference:
        from .proposal_sheets import build_proposal_sheets
        summary['contact_sheets']=build_proposal_sheets(out)
        atomic_json(out/'analysis.json',summary)
    return summary
