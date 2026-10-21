"""Analyze saved commissioning tiles without moving hardware or assigning thickness."""
from pathlib import Path
from dataclasses import asdict
import sys,json
import cv2,numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.imaging import load_frame,preview
from flakefinder.model import CameraTransform,atomic_json
from flakefinder.autofocus import make_focus_mask
from flakefinder.segmentation import segment_flakes
from flakefinder.scan import deduplicate,export_csv
from flakefinder.report import write_report
folder=Path('data/hardware-survey-local-02')
manifest=json.loads((folder/'manifest.json').read_text())
transform=CameraTransform(**manifest['transform']);rows=[]
for path in sorted((folder/'tiles').glob('*.tiff')):
    f=load_frame(path);mask=make_focus_mask(f.rgb)
    flakes,segmentation,quality=segment_flakes(f,transform,substrate_mask=mask)
    overlay=preview(f.rgb)
    for flake in flakes:
        flake.id=path.stem+'-'+flake.id;flake.tile_id=path.stem
        cv2.polylines(overlay,[np.int32(flake.polygon_px)],True,(255,210,0),2)
    result=[asdict(v) for v in flakes];rows.extend(result)
    atomic_json(path.with_suffix('.analysis.json'),{'flakes':result,'quality':quality})
    cv2.imwrite(str(path.with_suffix('.overlay.jpg')),overlay[:,:,::-1])
    cv2.imwrite(str(path.with_suffix('.analysis-mask.png')),segmentation)
    print(path.stem,len(flakes),quality,flush=True)
rows=deduplicate(rows)
atomic_json(folder/'flakes.json',{'flakes':rows,'source':'Offline analysis of real commissioning tiles; no layer calibration'})
export_csv(folder/'flakes.csv',rows);write_report(folder)
print('Total optical candidates',len(rows),flush=True)
