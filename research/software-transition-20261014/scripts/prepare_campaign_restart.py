"""Prepare a new same-coordinate restart from reviewed saved evidence; no hardware."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.campaign_restart import create_restart_plan

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--campaign',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--replay',type=Path,required=True);p.add_argument('--coordinate-frame-id',required=True)
    p.add_argument('--attempts',nargs='+',default=['candidate010','candidate015'])
    a=p.parse_args();r=create_restart_plan(a.campaign,a.output,a.replay,expected_coordinate_frame_id=a.coordinate_frame_id,attempts=a.attempts)
    print(json.dumps({'restart_id':r['restart_id'],'tasks':[{k:t[k]for k in ('id','kind','status','tile_count')if k in t}for t in r['tasks']],
        'remaining_lattice_probes':len(r['remaining_search']['indices']),'all_chips_found':False},indent=2))

if __name__=='__main__':main()
