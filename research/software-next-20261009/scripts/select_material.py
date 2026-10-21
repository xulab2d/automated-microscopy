"""Reclassify saved exact masks using a fixed optical-reference profile. No hardware."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path,help='Saved analysis with observations and exact instance masks')
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    import cv2
    cv2.setNumThreads(2)
    from threadpoolctl import threadpool_limits
    from flakefinder.material_selection import classify_saved_analysis
    from flakefinder.review import refresh_review
    with threadpool_limits(limits=2):
        result=classify_saved_analysis(args.source,args.output,args.profile)
        review=refresh_review(args.output,args.source)
    print(json.dumps({**result,'review':str(review)},indent=2))

if __name__=='__main__':main()
