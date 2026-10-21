"""Run independent offline chip analyses in a bounded local process pool."""
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor,as_completed
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def analyze_chip(folder):
    import cv2
    cv2.setNumThreads(2)
    from threadpoolctl import threadpool_limits
    from flakefinder.thin_analysis import analyze_thin_scan
    with threadpool_limits(limits=2):
        result=analyze_thin_scan(folder,output=Path(folder)/'analysis-thin-contrast')
    return {'chip':Path(folder).name,**{k:result[k] for k in
        ('candidate_count','raw_regions','distinct_images','failed_tiles','reused_fields','analysis_seconds','total_seconds')}}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path)
    parser.add_argument('--workers',type=int,choices=range(1,5),default=2);args=parser.parse_args()
    chips=[]
    for manifest in sorted(args.folder.glob('*/manifest.json')):
        if json.loads(manifest.read_text(encoding='utf-8')).get('completed'):chips.append(manifest.parent.resolve())
    if not chips:raise ValueError('No saved chip fields to analyse.')
    results=[]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(analyze_chip,str(chip)):chip for chip in chips}
        for future in as_completed(futures):
            try:result=future.result()
            except Exception as exc:result={'chip':futures[future].name,'error':str(exc)}
            results.append(result);print(json.dumps(result),flush=True)
    from flakefinder.model import atomic_json
    from flakefinder.storage import resolve_output_path
    atomic_json(resolve_output_path(args.folder/'thin-analysis-batch.json'),{'workers':args.workers,'chips':results,
        'scope':'Previously acquired fields only; no hardware connection or new acquisition.'})

if __name__=='__main__':main()
