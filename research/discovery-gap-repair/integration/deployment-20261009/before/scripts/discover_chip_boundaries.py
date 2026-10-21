"""Generic substrate-seed search and measured chip-boundary acquisition.

plan-search performs no hardware access. edges/seed-search use one explicitly
configured, armed adapter. No grid shape or expected chip count is required.
"""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from flakefinder.chip_discovery import BoundaryOptions,discover_seed_enclosure,discover_substrate_seeds,plan_unknown_count_search
from flakefinder.sparse_acquisition import SparseEdgeOptions,discover_sparse_outline
from flakefinder.config import connect,arm,load_config,transform_from_config
from flakefinder.imaging import load_frame
from flakefinder.model import atomic_json
from flakefinder.motion import move_z_bounded


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    planning=commands.add_parser('plan-search',help='Write a count-independent sampling plan without connecting hardware')
    planning.add_argument('--bounds',nargs=4,type=float,required=True,metavar=('XMIN','XMAX','YMIN','YMAX'))
    planning.add_argument('--minimum-chip-diameter-um',type=float,required=True,help='Minimum unoccluded inscribed substrate disk; not chip bounding-box width')
    planning.add_argument('--detectable-patch-diameter-um',type=float,default=40)
    planning.add_argument('--max-probes',type=int,default=100000)
    planning.add_argument('--output',required=True,type=Path)
    for name in ('edges','seed-search'):
        sub=commands.add_parser(name)
        sub.add_argument('--config',required=True,type=Path)
        sub.add_argument('--reference',required=True,type=Path,help='Verified exposed-substrate raw TIFF from the same optical profile')
        sub.add_argument('--output',required=True,type=Path)
        sub.add_argument('--resume',action='store_true')
        sub.add_argument('--use-reference-height',action='store_true',help='Reuse saved reference Z once; geometric evidence only, not a focus-plane extrapolation')
        if name=='edges':
            sub.add_argument('--continue-unresolved',action='store_true',help='With --resume, reuse saved rays and refine ambiguous high-substrate brackets; preserve the total frame budget')
            sub.add_argument('--seed-xy',nargs=2,type=float,help='Verified substrate seed; defaults to reference frame XY')
            sub.add_argument('--bounds',nargs=4,type=float,required=True)
            sub.add_argument('--cell-um',type=float,default=10)
            sub.add_argument('--overlap',type=float,default=.20)
            sub.add_argument('--method',choices=('sparse','dense'),default='sparse',help='Sparse coarse rays and edge bracketing (default); dense is the explicit legacy frontier routine')
            sub.add_argument('--coarse-step-um',type=float,default=1500)
            sub.add_argument('--margin-um',type=float,default=75)
            sub.add_argument('--max-probes',type=int,default=32)
            sub.add_argument('--dark-holder-ratio',type=float,default=.15)
        else:sub.add_argument('--plan',required=True,type=Path)
    args=parser.parse_args()
    if args.command=='plan-search':
        plan=plan_unknown_count_search(args.bounds,args.minimum_chip_diameter_um,args.detectable_patch_diameter_um,args.max_probes)
        atomic_json(args.output,plan)
        print(json.dumps({'plan':str(args.output),'probes':plan['probe_count'],'expected_chip_count':None,'limits':plan['limits']}));return
    cfg=load_config(args.config);reference=load_frame(args.reference);transform=transform_from_config(cfg)
    policy=f'fixed saved reference height {reference.position.z:.6f} um; geometric evidence only' if args.use_reference_height else 'unchanged current height; geometric evidence only'
    fixed_applied=False
    def focus(scope,xy):
        nonlocal fixed_applied
        if args.use_reference_height and not fixed_applied:
            move_z_bounded(scope,reference.position.z);fixed_applied=True
    def progress(phase,frame,record):
        print(json.dumps({'phase':phase,'position_um':record['position_um'],
                          'classification':record.get('classification'),'fractions':record.get('fractions'),
                          'image':record['image']}),flush=True)
    scope=None
    try:
        scope=connect(cfg);arm(scope,cfg)
        if args.command=='edges':
            seed=args.seed_xy or [reference.position.x,reference.position.y]
            if args.method=='sparse':
                options=SparseEdgeOptions(args.bounds,args.coarse_step_um,args.max_probes,args.cell_um,args.dark_holder_ratio,args.margin_um,policy)
                result=discover_sparse_outline(scope,seed,transform,reference,args.output,options,context=cfg.get('context'),
                                               resume=args.resume,continue_unresolved=args.continue_unresolved,before_capture=focus,callback=progress)
            else:
                options=BoundaryOptions(args.bounds,args.cell_um,args.overlap,args.max_probes,
                                        dark_holder_ratio=args.dark_holder_ratio,focus_policy_id=policy)
                result=discover_seed_enclosure(scope,seed,transform,reference,args.output,options,context=cfg.get('context'),
                                               resume=args.resume,before_capture=focus,callback=progress)
            print(json.dumps({'state':result['state'],'probes':len(result['records']),
                              'estimated_outline':result.get('estimated_outline'),'enclosure':result.get('enclosure')},indent=2))
        else:
            plan=json.loads(args.plan.read_text())
            result=discover_substrate_seeds(scope,transform,reference,plan,args.output,context=cfg.get('context'),resume=args.resume,
                                            before_capture=focus,focus_policy_id=policy,callback=progress)
            print(json.dumps({'state':result['state'],'seeds':len(result['seeds']),'chip_count':None,'all_chips_found':False}))
    finally:
        if scope:scope.close()


if __name__=='__main__':main()
