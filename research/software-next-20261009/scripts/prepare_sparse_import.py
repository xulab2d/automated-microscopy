"""Revalidate saved sparse observations into a new lineage; never connect hardware."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.config import load_config,transform_from_config
from flakefinder.imaging import load_frame
from flakefinder.model import Limits
from flakefinder.sparse_import import prepare_discovery_import

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','output','reference','config','profile'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--additional-probes',type=int,default=0,help='Explicit NEW bounded allowance0..32; default only retains old unspent frames')
    a=p.parse_args();cfg=load_config(a.config)
    result=prepare_discovery_import(a.source,a.output,load_frame(a.reference),transform_from_config(cfg),Limits(**cfg['limits']),
        json.loads(a.profile.read_text(encoding='utf-8-sig')),additional_probes=a.additional_probes)
    print(json.dumps({'state':result['state'],'imported_frames':len(result['imported_records']),
        'budget':result['lineage'],'physical_placement_verified':False},indent=2))

if __name__=='__main__':main()
