"""Read saved JSON timing only; no microscope access or raw-image decoding."""
from pathlib import Path
import argparse,json,statistics,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.model import atomic_json

def read(path):return json.loads(path.read_text(encoding='utf-8'))
def summary(values):
    return {'count':len(values),'sum_s':sum(values),'median_s':statistics.median(values) if values else None}
def camera(meta):
    timing=meta.get('metadata',{}).get('acquisition_timing_s',{})
    return timing.get('capture_total_including_cleanup',timing.get('capture_total_before_cleanup'))
def audit(folder):
    folder=Path(folder);batch=read(folder/'batch.json');chips=[];attempts=[];search=[]
    for key in batch['chip_states']:
        manifest=read(folder/key/'manifest.json');metas=[read(folder/key/'tiles'/(name+'.json'))for name in manifest['completed']]
        times=[m['acquired_at']for m in metas];deltas=[b-a for a,b in zip(times,times[1:])]
        surface=read(folder/key/'focus-surface/focus-surface.json');anchors=surface.get('anchors',[])
        samples=sum(len(a.get(k,{}).get('samples',[]))for a in anchors for k in ('local_result','recovery_result','quick_result'))
        chips.append({'chip_id':key,'tiles':len(metas),'raster_capture_span_s':max(times)-min(times),
            'interfield_intervals':summary(deltas),'camera_calls':summary([v for m in metas if (v:=camera(m)) is not None]),
            'pre_raster_elapsed_including_any_pauses_s':min(times)-manifest['started_at'],
            'recorded_final_surface_samples':samples,'planned_area_acquired':manifest.get('planned_area_acquired'),
            'chip_extent_validated':manifest.get('chip_extent_validated')})
    for item in batch['boundary_attempts']:
        path=folder/'discovery'/Path(item['folder']).name/'discovery.json'
        if not path.exists():continue
        state=read(path);focus=state.get('edge_focus_attempts',[])
        attempts.append({'id':item['id'],'state':item['state'],'chip_id':item.get('chip_id'),
            'saved_probe_frames':len(state['records']),'elapsed_saved_s':state.get('updated_at',0)-state.get('started_at',0),
            'edge_focus_elapsed_s':sum(a.get('finished_at',a.get('started_at',0))-a.get('started_at',0)for a in focus),
            'edge_focus_samples':sum(len(a.get('result',{}).get('samples',[]))for a in focus),
            'seed_xy_um':state['seed_xy_um']})
    for rec in batch['seed_records']:
        if not rec.get('image') or rec.get('reference_reused'):continue
        meta=read((folder/rec['image']).with_suffix('.json'))
        search.append({'at':meta['acquired_at'],'classification':rec['classification'],'camera_s':camera(meta)})
    times=[v['at']for v in search];within=[]
    for a,b in zip(search,search[1:]):
        # Exclude spans containing chip work/discovery and pauses. This is a
        # bounded within-search cadence statistic, not a complete time ledger.
        if 0<b['at']-a['at']<30:within.append(b['at']-a['at'])
    from collections import Counter
    return {'schema':1,'coordinate_frame_id':batch['coordinate_frame_id'],'active_campaign_seconds':batch['elapsed_seconds'],
        'chips':chips,'attempts':attempts,'search':{'frames':len(search),'classifications':dict(Counter(x['classification']for x in search)),
            'camera_calls':summary([r['camera_s']for r in search if r['camera_s'] is not None]),
            'within_search_intervals_under30s':summary(within),'remaining_lattice_probes':batch.get('remaining_lattice_probes')},
        'totals':{'raster_tiles':sum(c['tiles']for c in chips),'raster_capture_span_s':sum(c['raster_capture_span_s']for c in chips),
            'discovery_elapsed_saved_s':sum(a['elapsed_saved_s']for a in attempts),'discovery_focus_s':sum(a['edge_focus_elapsed_s']for a in attempts),
            'unresolved_attempts':sum(a['state']=='unresolved'for a in attempts),'saved_discovery_probes':sum(a['saved_probe_frames']for a in attempts)},
        'qualification':'JSON/capture timestamps only. Discovery durations include intervening pauses; focus is a subset of discovery, not additive. Search short-gap cadence excludes interleaved chip work and pauses.'}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('campaign',type=Path);p.add_argument('--output',required=True,type=Path);a=p.parse_args()
    result=audit(a.campaign);atomic_json(a.output,result);print(json.dumps(result['totals'],indent=2))
