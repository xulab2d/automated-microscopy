"""Offline representative-field optical plateau analysis; never connects hardware."""
from pathlib import Path
import argparse
import json
import os
import sys

for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ.setdefault(name,'1')
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--tiles',nargs='+',required=True,help='Explicit tile IDs; pilot only, no implicit full rerun')
    parser.add_argument('--parent-analysis',type=Path,help='Checksummed accepted parent analysis; defaults to scan/analysis-staged-features')
    parser.add_argument('--experimental-global',action='store_true',help='Opt-in unvalidated global foreground experiment; not the default source catalog')
    parser.add_argument('--conditional-optics','--illustrative-optics',dest='conditional_optics',action='store_true',help='Explicitly add uncalibrated vendor-response/nuisance optical hypotheses; never layer labels')
    args=parser.parse_args()
    from flakefinder.storage import resolve_output_path
    output=resolve_output_path(args.output)
    import cv2
    cv2.setNumThreads(1)
    from flakefinder.terrace_analysis import analyze_terraces,write_terrace_review
    provider=None
    if args.conditional_optics:
        import numpy as np
        from flakefinder.mote2_optics import load_optical_model,make_optical_provider,conditional_ensemble
        model=load_optical_model(ROOT/'configs/materials/mote2-optics-285nm.json')
        ensemble=conditional_ensemble(model,np.arange(0,40.01,.25),ROOT/'configs/materials/data/Axiocam305_vendor_response.json')
        provider=make_optical_provider(ensemble)
    summary=analyze_terraces(args.folder,output,tile_ids=args.tiles,optical_provider=provider,parent_analysis=args.parent_analysis,
        experimental_global=args.experimental_global,callback=lambda key,state:print(key+' '+str(state['completed_tiles'])+' analysed',flush=True))
    write_terrace_review(output,args.folder)
    print(json.dumps({'output':str(output),**{k:summary[k] for k in ('completed_tiles','total_tiles','candidate_count','failed_tiles','excluded_tiles')}},indent=2))


if __name__=='__main__':main()
