"""Evaluate a saved exposure series; does not connect to the microscope.

Input JSON: {frames:[{path:"tile.tiff",exposure_ms:100},...],
             dark_frames:["dark0.tiff","dark1.tiff"],white_level:4095}.
Paths resolve beside the input JSON. Each exposure needs a repeat pair.
"""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('series',type=Path)
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    from flakefinder.imaging import load_frame
    from flakefinder.radiometry import assess_exposure_series
    from flakefinder.model import atomic_json
    data=json.loads(args.series.read_text(encoding='utf-8'));root=args.series.resolve().parent
    frames=[load_frame(root/entry['path']) for entry in data['frames']]
    dark=[load_frame(root/name) for name in data['dark_frames']] if data.get('dark_frames') else None
    report=assess_exposure_series(frames,[entry['exposure_ms'] for entry in data['frames']],dark,data.get('white_level',4095))
    atomic_json(args.output,report)
    print(json.dumps({'status':report['status'],'response_rgb':report['response_rgb'],
                      'preferred_setting':report['preferred_setting'],'output':str(args.output)},indent=2))

if __name__=='__main__':main()
