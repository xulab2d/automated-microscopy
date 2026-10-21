"""Offline local contrast on preserved source masks; never controls hardware."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.thin_existing import measure_saved_features


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source');parser.add_argument('output')
    args=parser.parse_args()
    def progress(index,total,tile,report):
        print(f'{index}/{total} {tile}: {report["measured_regions"]}/{report["regions"]} source regions measured',flush=True)
    result=measure_saved_features(args.source,args.output,callback=progress)
    print(json.dumps(result,indent=2),flush=True)
