"""Review a full-chip plan, then acquire it with substrate-supported focus."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from flakefinder.config import connect,arm,load_config,transform_from_config
from flakefinder.full_chip import FullChipOptions,build_full_chip_plan,run_full_chip
from flakefinder.imaging import load_frame
from flakefinder.model import atomic_json


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    planning=commands.add_parser('plan',help='Read saved boundary evidence and write the entire coverage/focus plan; no hardware')
    planning.add_argument('--discovery',type=Path,required=True)
    planning.add_argument('--chip-id')
    planning.add_argument('--outline-json',type=Path,help='Sparse conservative outline: polygon_um (already uncertainty-expanded), uncertainty_um, method; does not require a completed collar')
    planning.add_argument('--overlap',type=float,default=.15)
    planning.add_argument('--focus-roi-um',type=float,default=160)
    planning.add_argument('--focus-half-range-um',type=float,default=12)
    planning.add_argument('--max-local-focus-offset-um',type=float,default=600)
    planning.add_argument('--max-plane-extrapolation-um',type=float,default=1500,help='Use 0 to disable extrapolation; also limited to 25%% of measured anchor diameter')
    planning.add_argument('--max-plane-prediction-sigma-um',type=float,default=2)
    planning.add_argument('--anchor-count',type=int,default=4)
    planning.add_argument('--max-tiles',type=int,default=10000)
    running=commands.add_parser('run',help='Acquire a reviewed saved full-chip plan')
    running.add_argument('--plan',type=Path,required=True)
    running.add_argument('--prior-scan',type=Path,help='Successful same-sample/profile focus anchors from an earlier interior scan')
    running.add_argument('--resume',action='store_true')
    running.add_argument('--allow-source-change',action='store_true',help='Explicitly record a code repair while retaining identical plan/profile/limits and saved evidence')
    for sub in (planning,running):
        sub.add_argument('--config',type=Path,required=True)
        sub.add_argument('--reference',type=Path,required=True)
        sub.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();cfg=load_config(args.config);reference=load_frame(args.reference)
    from flakefinder.storage import resolve_output_path
    args.output=resolve_output_path(args.output)
    if args.command=='plan':
        interior=cfg.get('interior_scan',{})
        options=FullChipOptions(overlap=args.overlap,focus_roi_um=args.focus_roi_um,anchor_count=args.anchor_count,
            focus_half_range_um=args.focus_half_range_um,focus_step_um=interior.get('focus_step_um',3),
            focus_max_residual_um=interior.get('focus_max_residual_um',1),max_local_focus_offset_um=args.max_local_focus_offset_um,
            max_tiles=args.max_tiles,focus_search_bounds_um=interior.get('focus_search_bounds_um'),
            focus_max_extrapolation_um=args.max_plane_extrapolation_um,focus_max_prediction_sigma_um=args.max_plane_prediction_sigma_um)
        outline=json.loads(args.outline_json.read_text()) if args.outline_json else None
        plan=build_full_chip_plan(args.discovery,reference,transform_from_config(cfg),options,args.chip_id,estimated_outline=outline)
        atomic_json(args.output,plan)
        print(json.dumps({'plan':str(args.output),'bounds_um':plan['scan_plan']['bounds_um'],'tiles':plan['tile_count'],
                          'supported_anchors_um':plan['anchor_xy_um'],'focus_policy':plan['focus_policy'],'boundary_estimate':plan['boundary_estimate']},indent=2));return
    plan=json.loads(args.plan.read_text());scope=None
    def progress(phase,frame,info):print(json.dumps({'phase':phase,**info}),flush=True)
    try:
        scope=connect(cfg);arm(scope,cfg)
        result=run_full_chip(scope,plan,reference,args.output,context=cfg.get('context'),prior_scan=args.prior_scan,
                             resume=args.resume,allow_source_change=args.allow_source_change,callback=progress)
        print(json.dumps({'state':result['state'],'completed':len(result['completed']),'total':result['tile_count'],
                          'focus_limited_fields':len(result['focus_limits']),'planned_area_acquired':result.get('planned_area_acquired'),
                          'chip_extent_validated':result.get('chip_extent_validated')},indent=2))
    finally:
        if scope:scope.close()


if __name__=='__main__':main()
