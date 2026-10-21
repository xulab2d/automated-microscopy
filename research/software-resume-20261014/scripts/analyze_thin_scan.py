"""Analyze recorded chips for thin-contrast plateaus; no microscope connection."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--min-area',type=float,default=100.)
    parser.add_argument('--min-width',type=float,default=5.)
    args=parser.parse_args()
    import cv2
    cv2.setNumThreads(2)
    from threadpoolctl import threadpool_limits
    from flakefinder.thin_analysis import analyze_thin_scan
    with threadpool_limits(limits=2):
        result=analyze_thin_scan(args.source,{'min_area_um2':args.min_area,'min_width_um':args.min_width},args.output,
            lambda phase,p:print(f"{p['index']}/{p['total']} {phase}",flush=True))
    print(json.dumps({k:result[k] for k in ('candidate_count','raw_regions','distinct_images','failed_tiles','analysis_seconds','total_seconds','reused_fields')},indent=2))

if __name__=='__main__':main()
