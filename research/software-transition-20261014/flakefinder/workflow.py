"""Survey -> chip identities -> per-chip focused scans, independent of material."""
from pathlib import Path
from dataclasses import asdict
import json
import numpy as np
import cv2
from .model import SafetyError, AcquisitionError, Position, atomic_json
from .scan import ScanPlan, run_scan, identify_chips, export_csv
from .autofocus import autofocus
from .calibration import FocusMap
from .motion import move_z_bounded

def inset_polygon(polygon,margin):
    """Offset each convex chip edge inward by a physical margin."""
    p=cv2.convexHull(np.float32(polygon))[:,0,:].astype(float)
    centre=p.mean(axis=0); normals=[]; offsets=[]
    for a,b in zip(p,np.roll(p,-1,axis=0)):
        v=b-a; n=np.array([-v[1],v[0]])/np.linalg.norm(v)
        if np.dot(n,centre-a)<0: n=-n
        normals.append(n); offsets.append(np.dot(n,a)+margin)
    result=[]
    for i in range(len(p)):
        result.append(np.linalg.solve([normals[i-1],normals[i]],[offsets[i-1],offsets[i]]))
    result=np.array(result)
    if not np.all(result@np.array(normals).T>=np.array(offsets)-1e-5):
        raise AcquisitionError('Chip is too small for the specified edge margin.')
    return result

def scan_chips(scope,transform,survey_folder,output_folder,config,model=None,callback=None,resume=False):
    from .storage import managed_output_path
    chips=json.loads((managed_output_path(survey_folder)/'chips.json').read_text())['chips']
    if not chips: raise AcquisitionError('Identify the chips before starting detail scans.')
    output=managed_output_path(output_folder); output.mkdir(parents=True,exist_ok=True)
    options=dict(config.get('detail',{})); margin=options.pop('edge_margin_um',10)
    if not np.isfinite(margin) or margin<0: raise ValueError('Edge margin must be nonnegative.')
    # Validate every chip and every scan region before moving to the first one.
    plans=[]
    for chip in chips:
        p=inset_polygon(chip['stage_polygon_um'],margin)
        bounds=[float(p[:,0].min()),float(p[:,0].max()),float(p[:,1].min()),float(p[:,1].max())]
        for x,y in p: scope.limits.check(Position(x,y,scope.position().z))
        plans.append(ScanPlan(bounds,chip_id=chip['id'],polygon_um=p.tolist(),mode='detail',**options))
    state={'state':'running','chips':[p.chip_id for p in plans],'completed':[]}
    try:
        for plan in plans:
            scope.check_cancel()
            if callback: callback('chip',None,{'chip_id':plan.chip_id})
            manifest=run_scan(scope,plan,transform,output/plan.chip_id,model,config.get('context'),callback,resume)
            state['completed'].append(plan.chip_id)
            atomic_json(output/'batch.json',state)
        state['state']='complete'
    except BaseException as e:
        state['state']='failed'; state['error']=str(e); scope.stop(); raise
    finally:
        atomic_json(output/'batch.json',state)
        rows=[]
        for plan in plans:
            file=output/plan.chip_id/'flakes.json'
            if file.exists(): rows.extend(json.loads(file.read_text())['flakes'])
        atomic_json(output/'flakes.json',{'flakes':rows})
        export_csv(output/'flakes.csv',rows)
    if state['state']=='complete':
        from .analysis import analyze_scan
        summary=analyze_scan(output,config.get('analysis'),callback=(lambda phase,info:callback('analysis',None,{'phase':phase,**info})) if callback else None,
                             model=model,check_cancel=scope.check_cancel)
        state['analysis_folder']=str(output/'analysis');state['candidates']=summary['candidate_count']
        atomic_json(output/'batch.json',state)
    return state

def sample_focus_map(scope,points,focus_options,output,callback=None):
    """Measure explicit chip-interior anchors; a failed anchor stops the map."""
    output=Path(output); values=[]
    for i,(x,y) in enumerate(points):
        scope.travel(x,y)
        result=autofocus(scope,**focus_options,output=output/str(i),callback=callback)
        if not result.success: raise AcquisitionError(f'Focus anchor {i}: {result.reason}')
        values.append([x,y,result.z_um])
    fitted=FocusMap(values)
    atomic_json(output/'focus-map.json',{'points':values,'rms_um':fitted.rms_um})
    return fitted
