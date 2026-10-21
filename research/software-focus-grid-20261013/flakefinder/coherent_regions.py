"""Local contrast populations with spatial connectivity, not binary envelopes.

RGB density clustering identifies optical domains, not material identity. Every
reported region is one connected color population. Neighboring particles cannot
pool their areas through a binary foreground envelope or a filled outer contour.
"""
import cv2
import numpy as np

from .colors import ColorGroups


def coherent_region_labels(frame,prepared,options):
    initial=np.uint8(prepared['foreground'])
    count,envelopes,stats,_=cv2.connectedComponentsWithStats(initial,8)
    height,width=initial.shape;scale=min(frame.pixel_size_um)
    pixel_area=float(np.prod(frame.pixel_size_um))
    padding=max(4,int(np.ceil(8/scale)))
    result=np.zeros(initial.shape,np.int32);metadata={};next_label=1
    rng=np.random.default_rng(71)
    for envelope in range(1,count):
        x,y,w,h,area_px=map(int,stats[envelope])
        if area_px*pixel_area<options.min_detection_area_um2:continue
        x0=max(0,x-padding);y0=max(0,y-padding);x1=min(width,x+w+padding);y1=min(height,y+h+padding)
        origin=np.array([x0,y0]);local_envelopes=envelopes[y0:y1,x0:x1]
        seed=local_envelopes==envelope
        valid=prepared['valid'][y0:y1,x0:x1]
        rgb_contrast=prepared['smooth'][y0:y1,x0:x1]
        # Anchor the background to measured exposed-substrate pixels, including
        # those between particles. It is not a free foreground GMM component.
        background=valid&(local_envelopes==0)
        if np.count_nonzero(background)<30:continue
        background_values=rgb_contrast[background]
        centre=np.median(background_values,axis=0)
        local=rgb_contrast-centre
        # Balanced foreground/background samples keep a tiny optical terrace
        # from being overwhelmed by a much larger surrounding substrate area.
        values=[]
        for selection in (seed,background):
            sample=local[selection]
            if len(sample)>3000:sample=sample[rng.choice(len(sample),3000,replace=False)]
            values.append(sample)
        groups=ColorGroups.fit(np.concatenate(values),options.max_components)
        if len(groups.groups)<2:continue
        labels=groups.predict(local.reshape(-1,3))[0].reshape(local.shape[:2])
        bg_labels=labels[background]
        # The component most represented on exposed substrate is background.
        # Additional background modes are recognized by their overlap with the
        # measured substrate covariance, not by closeness to a material prior.
        primary=int(np.bincount(bg_labels[bg_labels>=0],minlength=len(groups.groups)).argmax())
        bg_core=background_values[bg_labels==primary]-centre
        covariance=np.cov(bg_core.T) if len(bg_core)>3 else np.diag(prepared['noise_rgb']**2)
        covariance=np.atleast_2d(covariance)+np.diag(np.maximum(prepared['noise_rgb'],.002)**2)
        inverse=np.linalg.inv(covariance)
        bg_centre=np.median(bg_core,axis=0) if len(bg_core) else np.zeros(3)
        background_ids={primary}
        for j,group in enumerate(groups.groups):
            delta=np.asarray(group['mean'])-bg_centre
            if float(delta@inverse@delta)<(options.substrate_sigma/2)**2:background_ids.add(j)
        # A looser growth threshold preserves optical edges below the initial
        # seed threshold. Growth must remain in the same color population and
        # connected to the original seed; no bounding-box filling is performed.
        delta=local-bg_centre
        distance=np.einsum('...i,ij,...j->...',delta,inverse,delta,optimize=True)
        support=valid&(distance>(options.substrate_sigma/2)**2)&(np.linalg.norm(delta,axis=2)>options.min_contrast/2)
        support&=(local_envelopes==0)|seed
        for j,group in enumerate(groups.groups):
            if j in background_ids:continue
            domain=np.uint8((labels==j)&support)
            pieces,components,component_stats,_=cv2.connectedComponentsWithStats(domain,8)
            for component in range(1,pieces):
                bx,by,bw,bh,pixels=map(int,component_stats[component])
                if pixels*pixel_area<options.min_detection_area_um2:continue
                piece=components[by:by+bh,bx:bx+bw]==component
                if not np.any(piece&seed[by:by+bh,bx:bx+bw]):continue
                destination=result[y0+by:y0+by+bh,x0+bx:x0+bx+bw]
                # Pixel ownership is exclusive across neighboring envelopes.
                available=piece&(destination==0)
                if np.count_nonzero(available)*pixel_area<options.min_detection_area_um2:continue
                destination[available]=next_label
                metadata[next_label]={'segmentation_method':'local_contrast_connected_domain',
                    'source_envelope_id':int(envelope),'source_envelope_bbox_px':[x,y,w,h],
                    'source_envelope_area_um2':float(area_px*pixel_area),
                    'local_color_mean_rgb':(np.asarray(group['mean'])+centre).tolist(),
                    'local_background_contrast_rgb':centre.tolist(),
                    'local_background_population':primary,'local_color_population':j,
                    'local_color_population_count':len(groups.groups)}
                next_label+=1
    return result,metadata
