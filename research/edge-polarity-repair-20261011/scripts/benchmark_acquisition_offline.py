"""Saved JSON/curve replay only; no raw loading, SDK import or hardware client."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
from flakefinder.autofocus import fit_quick_focus_curve

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-workspace',required=True);p.add_argument('--output',required=True)
    args=p.parse_args();source=Path(args.source_workspace);out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    timing_path=source/'research/restart-speed-audit/timing.json'
    camera_path=source/'research/camera-latency-audit-01/measured-stream-arrival.json'
    timing=read(timing_path);camera=read(camera_path);campaign=source/'data/whole-chip-standardized-01'
    rows=[]
    paths=sorted(set(campaign.glob('chip*/focus-surface/**/focus-curve.json'))|set(campaign.glob('discovery/*/edge-focus/**/focus-curve.json')))
    for path in paths:
        data=read(path);samples=data.get('samples',data) if isinstance(data,dict) else data
        fine=[r for r in samples if r.get('phase')=='fine'];coarse=[r for r in samples if r.get('phase')=='coarse']
        if len(fine)<=6 or len(coarse)<3:continue
        fine=sorted(fine,key=lambda r:r['z_um']);centre=max(coarse,key=lambda r:r['score'])['z_um']
        # Existing dense fine samples approximate the five new requested Zs;
        # retain exact original sampled readbacks, never invent observations.
        targets=np.linspace(centre-2,centre+2,5)
        five=[min(fine,key=lambda r:abs(r['z_um']-z)) for z in targets]
        if len({r['z_um'] for r in five})!=5 or max(abs(r['z_um']-z) for r,z in zip(five,targets))>.30:continue
        result=fit_quick_focus_curve(five);confirmation=None
        if result.success:
            confirmation=float(np.interp(result.z_um,[r['z_um'] for r in fine],[r['score'] for r in fine]))
        passes=bool(result.success and confirmation>=.95*max(r['score'] for r in five))
        dense_peak=max(fine,key=lambda r:r['score'])['z_um']
        proposed=6 if passes else len(fine)+6
        rows.append({'source':str(path.relative_to(source)),'source_sha256':sha(path),'dense_fine_frames':len(fine),
            'modeled_new_fine_frames':proposed,'modeled_frames_saved':len(fine)-proposed,'fit_accepted':result.success,
            'modeled_confirmation_pass':passes,'fitted_peak_um':result.z_um,'dense_peak_um':dense_peak,
            'peak_difference_um':abs(result.z_um-dense_peak),'reason':result.reason})
    tail=camera['stats']['completion_after_image']['median_s']
    chip_rows=[]
    for chip in timing['chips']:
        matching=[r for r in rows if ('/'+chip['chip_id']+'/') in r['source'].replace('\\','/')]
        saved=sum(r['modeled_frames_saved'] for r in matching)
        chip_rows.append({**chip,'modeled_focus_frames_saved_across_recorded_attempts':saved,
            'modeled_camera_time_saved_s':saved*chip['camera_calls']['median_s'],
            'raster_policy':'No raster frame reduction or motion-time speedup claimed; LZW and all tiles retained.'})
    failed=[r for r in timing['attempts'] if r.get('state')=='unresolved']
    report={'schema':1,'kind':'offline_replay_and_conditional_time_model','source_sha256':{str(timing_path):sha(timing_path),str(camera_path):sha(camera_path)},
        'measured':{'active_campaign_seconds':timing['active_campaign_seconds'],'chips':chip_rows,
            'raster_capture_span_seconds':sum(r['raster_capture_span_s'] for r in timing['chips']),
            'camera_first_frame_tail':camera['stats']['completion_after_image'],'failed_discovery_attempts':len(failed),
            'failed_discovery_saved_elapsed_s':sum(r['elapsed_saved_s'] for r in failed),'search':timing['search']},
        'adaptive_fine_replay':{'curves':len(rows),'fit_and_modeled_confirmation_passes':sum(r['modeled_confirmation_pass'] for r in rows),
            'dense_fine_frames':sum(r['dense_fine_frames'] for r in rows),'modeled_new_fine_frames':sum(r['modeled_new_fine_frames'] for r in rows),
            'modeled_frames_saved':sum(r['modeled_frames_saved'] for r in rows),'rows':rows},
        'future_bounds':{'anchor_focus':{'captures':48,'seconds':180,'additional_selection_and_final_anchor_frames':2},
            'discovery':{'geometric_frames':32,'total_captures_including_focus':64,'seconds':240},
            'time_semantics':'Cooperative: in-flight call/cleanup may overrun; late focus cannot be accepted.'},
        'limitations':['Fine replay subsamples measured curves; confirmation is interpolated, not a new independent capture. Live confirmation remains mandatory.',
            'Saved attempt durations may include pauses. Capping them at240s is not a measured time saving and may leave a chip unresolved.',
            'Processor overlap can hide at most the remaining measured tail, median%.3fs; actual benefit is bounded by CPU work supplied. No frame is returned before final job success/unload.'%tail,
            'No overall speed multiplier promised. Failed/repeated discovery and empty-space search dominate additional work; corrected geometry must be validated on fresh placement.']}
    out.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'raster_minutes':report['measured']['raster_capture_span_seconds']/60,
        'failed_discovery_minutes':report['measured']['failed_discovery_saved_elapsed_s']/60,
        'fine_curves':len(rows),'passed':report['adaptive_fine_replay']['fit_and_modeled_confirmation_passes'],
        'modeled_frames_saved':report['adaptive_fine_replay']['modeled_frames_saved'],'first_frame_tail_median_s':tail},indent=2))
if __name__=='__main__':main()
