"""Offline research: independently validate a label-derived boundary in raw RGB.

Specific to the recorded dark-holder profile. These checks do not support bright
holders or infer unobserved edges. All returned coordinates are image pixels;
the original candidate remains unchanged when it passes validation.
"""
import cv2
import numpy as np


def validate_photometric_edge(frame, edge, matrix):
    sh, sw = frame.rgb.shape[:2]
    scale = min(1., 512/max(sh, sw)); w, h = round(sw*scale), round(sh*scale)
    rgb = cv2.resize(frame.rgb.astype(np.float32), (w,h), interpolation=cv2.INTER_AREA)
    rgb = cv2.GaussianBlur(rgb, (0,0), 1)
    linear = np.asarray(matrix) @ np.diag([sw/w,sh/h])
    origin = np.array([frame.position.x,frame.position.y]) + np.asarray(matrix) @ [(sw/w-sw)/2,(sh/h-sh)/2]
    ns = np.asarray(edge['normal']); image_normal = linear.T @ ns
    factor = np.linalg.norm(image_normal); image_normal /= factor
    offset = (edge['offset_um']-origin@ns)/factor
    tangent = np.array([-image_normal[1],image_normal[0]])
    middle = np.array([(w-1)/2,(h-1)/2]); middle += image_normal*(offset-middle@image_normal)
    # Intersect the proposed line with the field, then test independent normal
    # profiles along it. The 50um search does not count as geometric evidence.
    intersections=[]
    for axis,upper in enumerate([w-1,h-1]):
        if abs(tangent[axis])<1e-9:continue
        for limit in (0,upper):
            t=(limit-middle[axis])/tangent[axis];point=middle+t*tangent
            if 0<=point[1-axis]<=[h-1,w-1][axis]:intersections.append(t)
    base={'accepted':False,'candidate_unchanged':True,'method':'raw_RGB_dark_step_profiles',
          'profile_assumption':'Holder is darker than exposed substrate in each recorded camera channel.'}
    if len(intersections)<2:return dict(base,reason='Candidate does not cross the camera field.')
    t0,t1=min(intersections),max(intersections)
    offsets=np.arange(-int(np.ceil(50/factor)),int(np.ceil(50/factor))+1,dtype=float)
    points=[];widths=[];deltas=[];tested=0
    for t in np.linspace(t0,t1,64)[2:-2]:
        centre=middle+t*tangent;coords=centre+offsets[:,None]*image_normal
        valid=(coords[:,0]>=1)&(coords[:,0]<w-2)&(coords[:,1]>=1)&(coords[:,1]<h-2)
        if valid.sum()<25:continue
        coords=coords[valid];q=offsets[valid];tested+=1
        profile=cv2.remap(rgb,coords[:,0].astype(np.float32)[None,:],coords[:,1].astype(np.float32)[None,:],cv2.INTER_LINEAR)[0]
        g=cv2.GaussianBlur(profile[:,1,None],(0,0),1.5)[:,0]
        derivative=-np.gradient(g)
        noise=1.4826*np.median(abs(derivative-np.median(derivative)))
        valid_peak=np.arange(len(q));valid_peak=valid_peak[(valid_peak>=10)&(valid_peak<len(q)-10)]
        if not len(valid_peak):continue
        peak=int(valid_peak[np.argmax(derivative[valid_peak])])
        if derivative[peak]<=6*max(noise,.25):continue
        left=np.median(profile[peak-10:peak-5],axis=0);right=np.median(profile[peak+5:peak+10],axis=0)
        delta=left-right
        if not np.all(delta>0):continue
        # Transition width is measured, not a guessed alignment tolerance.
        half=derivative[peak]/2;lo=hi=peak
        while lo>0 and derivative[lo-1]>=half:lo-=1
        while hi<len(q)-1 and derivative[hi+1]>=half:hi+=1
        widths.append(float(q[hi]-q[lo]+1));points.append(coords[peak]);deltas.append(delta)
    base.update(tested_profiles=tested,supported_profiles=len(points))
    if len(points)<12:return dict(base,reason='Insufficient independent dark-step profiles.')
    points=np.asarray(points);span=float(np.ptp(points@tangent))
    if len(points)<.5*tested or span<.45*min(h,w):
        return dict(base,reason='Raw edge support does not span enough of the field.',raw_span_px=span)
    vx,vy,cx,cy=cv2.fitLine(points.astype(np.float32),cv2.DIST_HUBER,0,.01,.001).ravel()
    normal=np.array([-vy,vx]);normal*=1 if normal@image_normal>=0 else -1
    residual=(points-[cx,cy])@normal
    scatter=max(1.,1.4826*float(np.median(abs(residual-np.median(residual)))))
    inliers=abs(residual-np.median(residual))<=3*scatter
    raw_span=float(np.ptp(points[inliers]@np.array([-normal[1],normal[0]])))
    agreement_drift=raw_span*abs(float(normal@tangent))
    # Both orientations must agree within the measured raw-line scatter over
    # its observed support. A long wrong-angle line cannot pass on purity alone.
    allowed_drift=6*scatter
    displacement=abs(float(np.median(points@image_normal-offset)))
    transition_width=float(np.quantile(widths,.9))
    base.update(raw_normal_image=normal.tolist(),candidate_normal_image=image_normal.tolist(),
        raw_working_shape=[h,w],
        raw_span_px=raw_span,raw_scatter_px=scatter,orientation_drift_px=agreement_drift,
        allowed_orientation_drift_px=allowed_drift,median_candidate_displacement_px=displacement,
        raw_transition_width_px_p90=transition_width,inlier_profiles=int(inliers.sum()),
        median_inside_minus_outside_RGB=np.median(deltas,axis=0).tolist(),
        measured_raw_points_image=points[inliers].tolist())
    if inliers.sum()<.5*tested:return dict(base,reason='Raw transition points are not a coherent line.')
    if scatter>transition_width:
        return dict(base,reason='Raw line scatter exceeds its measured transition width; possible occlusion or multiple edges.')
    if agreement_drift>allowed_drift:return dict(base,reason='Candidate direction disagrees with the measured raw edge.')
    if displacement>transition_width+3*scatter:return dict(base,reason='Candidate lies outside the measured raw transition.')
    return dict(base,accepted=True,reason='Label line independently supported by a broadband dark edge.')


def supported_edge(frame, labels, edge, matrix, *, allow_raw_refinement=False):
    """Keep agreeing candidates; refine only coherent independently measured data.

    Callers must explicitly opt in for newly recovered proposals. Previously
    measured edges are never silently replaced. Raw refinement cannot rescue
    insufficient dark-step support, large scatter, or a contradictory profile.
    """
    validation=validate_photometric_edge(frame,edge,matrix)
    if validation['accepted']:
        return edge,validation
    refinable=validation['reason'] in (
        'Candidate direction disagrees with the measured raw edge.',
        'Candidate lies outside the measured raw transition.')
    if not allow_raw_refinement or not refinable:return None,validation
    points=np.asarray(validation['measured_raw_points_image']);h,w=validation['raw_working_shape']
    sh,sw=frame.rgb.shape[:2];matrix=np.asarray(matrix)
    linear=matrix@np.diag([sw/w,sh/h]);origin=np.array([frame.position.x,frame.position.y])+matrix@[(sw/w-sw)/2,(sh/h-sh)/2]
    stage=points@linear.T+origin
    normal=np.linalg.solve(linear.T,np.asarray(validation['raw_normal_image']));normal/=np.linalg.norm(normal)
    offset=float(np.median(stage@normal))
    lh0,lw0=labels.shape;scale=min(1.,320/max(lh0,lw0));lh,lw=round(lh0*scale),round(lw0*scale)
    a=cv2.resize(labels,(lw,lh),interpolation=cv2.INTER_NEAREST);yy,xx=np.mgrid[:lh,:lw]
    label_linear=matrix@np.diag([sw/lw,sh/lh]);label_origin=np.array([frame.position.x,frame.position.y])+matrix@[(sw/lw-sw)/2,(sh/lh-sh)/2]
    image_normal=label_linear.T@normal;norm=np.linalg.norm(image_normal)
    distance=(xx*image_normal[0]+yy*image_normal[1]+label_origin@normal-offset)/norm
    inside,outside=distance< -4,distance>4
    ps=float(np.mean(a[inside]==1))if inside.any()else 0.
    ph=float(np.mean(a[outside]==2))if outside.any()else 0.
    if min(inside.mean(),outside.mean())<.07 or min(ps,ph)<.78:
        return None,dict(validation,reason='Raw-refined line fails unchanged label-side support gates.',refined_substrate_purity=ps,refined_holder_purity=ph)
    refined=dict(normal=normal.tolist(),offset_um=offset,points_um=stage.tolist(),substrate_purity=ps,holder_purity=ph,
        local_residual_px_p95=float(np.quantile(abs((points-points.mean(axis=0))@np.asarray(validation['raw_normal_image'])),.95)),
        proposal_method='label_proposal_refined_from_independent_raw_RGB_dark_steps',raw_working_shape=[h,w])
    return refined,dict(validation,accepted=True,candidate_unchanged=False,
        reason='Replaced label proposal by coherent raw edge points; unchanged label-side gates passed.',
        refined_substrate_purity=ps,refined_holder_purity=ph)


def spatial_bracket_evidence(labels, original_side):
    """Search-bracket evidence only; no seed, chip-boundary or focus acceptance."""
    if original_side in ('substrate','holder'):
        return {'side':original_side,'method':'original_classification','inferred':False}
    h,w=labels.shape;sf=float(np.mean(labels==1));hf=float(np.mean(labels==2))
    result={'side':'unknown','method':'whole_field_connected_support','inferred':False,
            'substrate_fraction':sf,'holder_fraction':hf,
            'qualification':'Search bracket only; does not relabel the centre, establish an edge, or verify focus.'}
    # Reuse existing90%-substrate /95%-holder reference-patch standards; require
    # spatially connected support rather than a mere aggregate colour fraction.
    for value,name,enough in ((1,'substrate',sf>=.9 and hf<=.05),(2,'holder',hf>=.95 and sf<=.05)):
        if not enough:continue
        n,components,stats,_=cv2.connectedComponentsWithStats(np.uint8(labels==value),8)
        if n<2:continue
        index=1+int(np.argmax(stats[1:,cv2.CC_STAT_AREA]));mask=components==index
        fractions=[float(mask[ys,xs].mean())for ys,xs in
            [(slice(0,h//2),slice(0,w//2)),(slice(0,h//2),slice(w//2,None)),
             (slice(h//2,None),slice(0,w//2)),(slice(h//2,None),slice(w//2,None))]]
        if min(fractions)>=.8:
            return dict(result,side=name,inferred=True,connected_quadrant_fractions=fractions)
    return result


def registered_substrate_consistency(line, observations, matrix, source_shape, margin_um=75.,patch_diameter_um=40.):
    """Reject a half-plane that excludes independently observed substrate patches.

    Observations are (name, labels, actual_xy); only >=90% substrate circular
    patches count, using the campaign's recorded40um detectable-patch scale.
    This tests the separated-convex-chip hypothesis; it does not establish it.
    """
    sh,sw=source_shape[:2];matrix=np.asarray(matrix);violations=[]
    for name,labels,xy in observations:
        h,w=labels.shape;linear=matrix@np.diag([sw/w,sh/h])
        radius=max(1,int(np.ceil(patch_diameter_um/2/max(np.linalg.norm(linear,axis=0)))))
        kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*radius+1,2*radius+1)).astype(np.float32);kernel/=kernel.sum()
        support=cv2.filter2D(np.float32(labels==1),-1,kernel,borderType=cv2.BORDER_CONSTANT)
        step=max(1,2*radius+1);yy,xx=np.mgrid[radius:h-radius:step,radius:w-radius:step]
        good=support[yy,xx]>=.9
        pixel=np.column_stack([xx[good],yy[good]])
        if not len(pixel):continue
        origin=np.asarray(xy)+matrix@[(sw/w-sw)/2,(sh/h-sh)/2]
        stage=pixel@linear.T+origin
        excess=stage@np.asarray(line['normal'])-line['offset_um']-margin_um
        if np.count_nonzero(excess>0)>=3:
            violations.append({'image':name,'excluded_substrate_patches':int(np.count_nonzero(excess>0)),
                               'tested_patches':len(pixel),'max_excess_beyond_margin_um':float(excess.max())})
    return {'accepted':not violations,'violations':violations,'margin_um':margin_um,
            'patch_diameter_um':patch_diameter_um,'hypothesis':'Same separated convex chip across the supplied registered observations.'}


ALGORITHM_VERSION = 'sparse_photometric_halfplanes_v2'
PROFILE_MODEL = 'dark_holder_all_rgb_v1'


def assess_edge_profile(profile, frame):
    """An explicit measured optical contract, never an inferred holder model.

    This first implementation is qualified only for the manually selected 20x
    objective. Supporting another objective/holder requires a reviewed profile
    and representative replay, not changing a magnification field at runtime.
    """
    from .layout import _context
    report = {'supported': False, 'algorithm': ALGORITHM_VERSION,
              'profile_assumption': 'Manual encoded objective at 20x; holder darker than substrate in all recorded RGB channels.'}
    if not isinstance(profile, dict) or profile.get('schema') != 1 or not profile.get('profile_id'):
        return dict(report, reason='An explicit versioned edge-validation profile is required.')
    if profile.get('holder_model') != PROFILE_MODEL:
        return dict(report, reason='Unsupported holder model; bright/unknown holders remain unresolved.')
    if profile.get('manual_objective') is not True or profile.get('objective_magnification') != 20:
        return dict(report, reason='This validator is qualified only for an explicitly selected manual 20x profile.')
    context = _context(frame)
    objective = context.get('objective') or {}
    if objective.get('magnification') != 20 or context.get('objective_position') != profile.get('objective_position'):
        return dict(report, reason='Measured objective does not match the declared manual 20x profile.')
    if any(context.get(key) is None for key in ('camera_resources', 'camera_job_template', 'white_level', 'illumination_percent')):
        return dict(report, reason='Camera, white-level and illumination metadata are required.')
    if context != profile.get('optical_context'):
        return dict(report, reason='Recorded camera/illumination/objective context differs from the declared profile.')
    return dict(report, supported=True, profile_id=profile['profile_id'], reason='Explicit optical contract matches recorded metadata; individual raw edges still require validation.')
