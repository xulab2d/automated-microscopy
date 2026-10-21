"""Derive final edge coordinates from saved verified anchors; no hardware."""
from pathlib import Path
import argparse,json,sys
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.imaging import load_frame
from flakefinder.full_chip import edge_focus_roi
from flakefinder.model import CameraTransform,atomic_json
from flakefinder.chip_discovery import file_hash


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign',type=Path)
    parser.add_argument('--chip',default='chip001')
    args=parser.parse_args();root=args.campaign
    cfg=json.loads((root/'configuration.json').read_text());transform=CameraTransform(**cfg['camera_transform'])
    focus=root/args.chip/'focus-surface';selected={}
    for report in sorted(focus.glob('attempt-*/focus-surface.json'),key=lambda p:p.stat().st_mtime):
        state=json.loads(report.read_text())
        for anchor in state.get('anchors',[]):
            if anchor.get('status')!='verified' or not anchor.get('image'):continue
            image=focus/anchor['image'];frame=load_frame(image)
            if frame.metadata.get('focus_mask_policy')!='include_chip_edge':continue
            key=tuple(np.round(anchor['requested_xy_um'],1));geometry=edge_focus_roi(frame,16,transform)
            xyz=[*geometry['point_xy_um'],frame.position.z]
            selected[key]={'image':str(image),'image_sha256':file_hash(image),'metadata_sha256':file_hash(image.with_suffix('.json')),
                'requested_xy_um':anchor['requested_xy_um'],'recorded_xyz_um':[frame.position.x,frame.position.y,frame.position.z],
                'derived_edge_xyz_um':xyz,'edge_xy_offset_um':(np.asarray(xyz[:2])-[frame.position.x,frame.position.y]).tolist(),
                'geometry':geometry,'independent_new_focus_measurement':False,
                'derivation':'Same successful saved focus Z, with XY assigned to the final visible edge rather than the earlier ROI centre.'}
    rows=list(selected.values());report={'schema':1,'anchor_count':len(rows),'anchors':rows,'original_evidence_modified':False}
    for field,name in [('recorded_xyz_um','original_plane'),('derived_edge_xyz_um','registered_plane')]:
        a=np.asarray([r[field] for r in rows],float)
        if len(a)<3 or np.linalg.matrix_rank(a[:,:2]-a[:,:2].mean(axis=0))<2:continue
        origin=a[:,:2].mean(axis=0);design=np.column_stack([a[:,:2]-origin,np.ones(len(a))])
        c=np.linalg.lstsq(design,a[:,2],rcond=None)[0];res=a[:,2]-design@c
        # Exact depth gradient over this camera's calibrated parallelogram.
        h,w=json.loads((root/args.chip/'coverage-plan.json').read_text())['camera_shape'][:2]
        camera_gradient=c[:2]@np.asarray(transform.matrix)
        span=float(abs(camera_gradient[0])*(w-1)+abs(camera_gradient[1])*(h-1))
        report[name]={'origin_xy_um':origin.tolist(),'coefficients_z_um_per_xy_um':c.tolist(),
            'gradient_um_per_mm':(c[:2]*1000).tolist(),'tilt_degrees':float(np.degrees(np.arctan(np.linalg.norm(c[:2])))),
            'residuals_um':res.tolist(),'max_abs_residual_um':float(np.max(abs(res))),
            'rms_residual_um':float(np.sqrt(np.mean(res**2))),'calibrated_full_field_z_span_um':span,
            'independent_plane_residual_check':len(rows)>=4}
    destination=root/'calibration/edge-registration/anchor-registration-audit.json'
    atomic_json(destination,report)
    print(json.dumps({k:v for k,v in report.items() if k!='anchors'},indent=2))
    for row in rows:print('anchor',row['recorded_xyz_um'],'derived',row['derived_edge_xyz_um'])


if __name__=='__main__':main()
