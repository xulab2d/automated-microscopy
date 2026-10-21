"""Read-only replay of committed boundary frames; never imports hardware."""
from pathlib import Path
import hashlib,json,sys
import cv2,numpy as np
from flakefinder.imaging import load_frame
from flakefinder.sparse_edges import validated_coherent_edge
from flakefinder.photometric_edges import MEASURED_PROFILE_MODEL

cv2.setNumThreads(1)
root=Path(sys.argv[1]);out=Path(sys.argv[2]);rows=[]
for folder in sorted((root/'discovery').glob('candidate*')):
    path=folder/'discovery.json'
    if not path.exists():continue
    state=json.loads(path.read_text())
    profile=dict(state['edge_validation_profile'],holder_model=MEASURED_PROFILE_MODEL)
    matrix=np.asarray(state['transform']['matrix'])
    for rec in state['records']:
        # Only frames which already have a committed hash enter this replay.
        image=folder/rec['image'];digest=hashlib.sha256(image.read_bytes()).hexdigest()
        assert digest==rec['hashes'][rec['image']]
        frame=load_frame(image)
        with np.load(folder/rec['labels_file']) as stored:labels=stored['labels']
        edge,report=validated_coherent_edge(frame,labels,matrix,profile)
        rows.append(dict(candidate=folder.name,name=rec['name'],phase=rec['phase'],image_sha256=digest,
            before=bool(rec['edge']),before_reason=rec['edge_validation'].get('reason'),
            after=bool(edge),after_reason=report.get('reason'),polarity=report.get('step_polarity'),
            supported=report.get('supported_profiles'),tested=report.get('tested_profiles'),
            inliers=report.get('inlier_profiles'),scatter=report.get('raw_scatter_px'),
            drift=report.get('orientation_drift_px'),allowed_drift=report.get('allowed_orientation_drift_px'),
            line=edge))
        del frame,labels
out.write_text(json.dumps(dict(hardware_access=False,run=str(root),rows=rows),indent=2))
print(json.dumps([dict(r, line=None) for r in rows if r['after'] or r['before'] or r.get('tested')]))
