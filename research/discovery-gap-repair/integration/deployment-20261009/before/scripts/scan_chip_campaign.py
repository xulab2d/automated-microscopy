"""Plan or run fresh-coordinate, interleaved chip discovery and full scans."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from flakefinder.chip_campaign import CampaignOptions,plan_campaign_search,run_chip_campaign
from flakefinder.config import load_config
from flakefinder.imaging import load_frame
from flakefinder.model import atomic_json


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=('plan','run'));p.add_argument('--config',required=True)
    p.add_argument('--reference',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--bounds',nargs=4,type=float,required=True,metavar=('XMIN','XMAX','YMIN','YMAX'))
    p.add_argument('--minimum-chip-diameter-um',type=float)
    p.add_argument('--max-chips',type=int);p.add_argument('--walltime-minutes',type=float)
    p.add_argument('--detectable-patch-diameter-um',type=float)
    p.add_argument('--boundary-radius-um',type=float);p.add_argument('--boundary-max-probes',type=int)
    p.add_argument('--boundary-coarse-step-um',type=float)
    p.add_argument('--initial-discovery',type=Path,help='Finished fresh sparse discovery to reuse before the next chip search.')
    p.add_argument('--seed-focus-z-um',type=float,help='Explicit fresh starting focus Z; default preserves actual start readback, never color-reference Z')
    p.add_argument('--seed-focus-evidence',type=Path,help='Successful fresh focus-result JSON matching current Z')
    p.add_argument('--seed-focus-frame',type=Path,help='Optional raw image at the fresh starting focus; hashes retained')
    p.add_argument('--seed-focus-bundle',type=Path,help='JSON array of fresh {result, frame, purpose} pairs; purpose search_seed initializes searches only, default verified supplies measured support; independent of discovery starting Z')
    p.add_argument('--resume',action='store_true',help='Resume the saved interrupted chip in place; preserve tiles/focus/discovery and stop with later discovery pending')
    p.add_argument('--continue-discovery',action='store_true',help='Continue the saved lattice after completed chip acquisition; retain fresh coordinate frame, evidence and active elapsed time')
    p.add_argument('--continuation-max-chips',type=int,help='Explicit revised total chip-attempt budget; only with --continue-discovery')
    p.add_argument('--continuation-hours',type=float,help='Additional active runtime from this continuation, excluding pauses; audited budget revision')
    p.add_argument('--allow-source-change',action='store_true',help='With --resume or --continue-discovery, audit a source-code transition while requiring unchanged geometry/profile')
    p.add_argument('--recover-seed-rejection',action='store_true',help='Narrow audited recovery of the historical seed-only classification failure; requires continuation/source flag and forbids budget renewal')
    p.add_argument('--full-chip-options',type=Path,help='Optional JSON containing fresh FullChipOptions only; no old coordinate priors.')
    args=p.parse_args();cfg=load_config(args.config)
    if args.resume and args.continue_discovery:p.error('Choose --resume or --continue-discovery')
    continuing=args.resume or args.continue_discovery
    if args.command=='plan' and continuing:p.error('Continuation/resume applies only to run')
    if continuing:
        values=json.loads((args.output/'batch.json').read_text(encoding='utf-8'))['options']
    else:
        if args.minimum_chip_diameter_um is None:p.error('A new campaign requires --minimum-chip-diameter-um')
        values={'minimum_chip_diameter_um':args.minimum_chip_diameter_um}
    for key in ('minimum_chip_diameter_um','max_chips','detectable_patch_diameter_um','boundary_radius_um','boundary_max_probes','boundary_coarse_step_um'):
        value=getattr(args,key)
        if value is not None:values[key]=value
    if args.walltime_minutes is not None:values['walltime_budget_s']=args.walltime_minutes*60
    options=CampaignOptions(**values)
    if args.command=='plan':
        reference=load_frame(args.reference)
        plan=plan_campaign_search(args.bounds,options.minimum_chip_diameter_um,[reference.position.x,reference.position.y],options.detectable_patch_diameter_um,options.max_seed_probes)
        atomic_json(args.output,plan);print(json.dumps({'plan':str(args.output),'lattice_probes':plan['probe_count'],'expected_chip_count':None}));return
    full=json.loads(args.full_chip_options.read_text(encoding='utf-8')) if args.full_chip_options else None
    seeds=None
    if args.seed_focus_bundle:
        seeds=json.loads(args.seed_focus_bundle.read_text(encoding='utf-8-sig'))
        if not isinstance(seeds,list):p.error('--seed-focus-bundle must contain a JSON array')
        for entry in seeds:
            if not isinstance(entry,dict) or not entry.get('result') or not entry.get('frame'):
                p.error('Each focus-bundle entry requires result and frame')
            for key in ('result','frame'):
                path=Path(entry[key])
                entry[key]=str(path.resolve() if path.is_absolute() else (args.seed_focus_bundle.resolve().parent/path).resolve())
    def progress(phase,frame,info):print(json.dumps({'phase':phase,**info}),flush=True)
    result=run_chip_campaign(cfg,args.reference,args.output,args.bounds,options,full_chip_options=full,
        initial_discovery=args.initial_discovery,seed_focus_z_um=args.seed_focus_z_um,
        seed_focus_evidence=args.seed_focus_evidence,seed_focus_frame=args.seed_focus_frame,seed_focus_results=seeds,callback=progress,
        resume=args.resume,allow_source_change=args.allow_source_change,continue_discovery=args.continue_discovery,
        recover_seed_rejection=args.recover_seed_rejection,
        continuation_max_chips=args.continuation_max_chips,
        continuation_walltime_s=None if args.continuation_hours is None else args.continuation_hours*3600)
    print(json.dumps({'state':result['state'],'chips':result['chip_states'],'all_chips_found':False},indent=2))


if __name__=='__main__':main()
