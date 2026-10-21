from pathlib import Path
import argparse,json,sys,time
from dataclasses import asdict
from .config import load_config,connect,arm,transform_from_config,save_transform,demo_config
from .model import atomic_json

def main():
    parser=argparse.ArgumentParser(description='FlakeFinder microscope acquisition and chip scanning. All distances are micrometres.')
    parser.add_argument('--config',default='configs/simulator.json')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('gui');sub.add_parser('status');sub.add_parser('stop')
    p=sub.add_parser('demo');p.add_argument('--output',default='data/demo-'+time.strftime('%Y%m%d-%H%M%S'))
    p=sub.add_parser('capture');p.add_argument('--output',required=True)
    p=sub.add_parser('focus');p.add_argument('--wide',action='store_true');p.add_argument('--output',required=True)
    sub.add_parser('calibrate')
    sub.add_parser('optics-status',help='Read actual objective positions and reflected-light setting')
    p=sub.add_parser('optimize-exposure');p.add_argument('--output',required=True);p.add_argument('--initial-ms',type=float,default=200);p.add_argument('--white-level',type=float,default=4095)
    p=sub.add_parser('objective-profile',help='Measure calibration on the currently engaged objective and save a revisit profile')
    p.add_argument('--output',required=True);p.add_argument('--use-existing-calibration',action='store_true')
    p=sub.add_parser('revisit',help='Acquire and independently verify a batch of candidate revisits')
    p.add_argument('folder');p.add_argument('--profile',required=True);p.add_argument('--output',required=True)
    p.add_argument('--selection',choices=['priority','all','kept'],default='priority');p.add_argument('--max-candidates',type=int);p.add_argument('--resume',action='store_true')
    for cmd in ['survey','detail']:
        p=sub.add_parser(cmd);p.add_argument('--output',required=True);p.add_argument('--resume',action='store_true')
        if cmd=='detail': p.add_argument('--survey',required=True)
    p=sub.add_parser('identify');p.add_argument('folder')
    p=sub.add_parser('report');p.add_argument('folder')
    p=sub.add_parser('analyze',help='Learn color groups and reprocess raw tiles without microscope motion')
    p.add_argument('folder');p.add_argument('--output');p.add_argument('--options',help='AnalysisOptions JSON file')
    p=sub.add_parser('review',help='Open the local crop, color-space and tile review interface')
    p.add_argument('folder');p.add_argument('--port',type=int,default=0);p.add_argument('--no-open',action='store_true')
    p=sub.add_parser('train');p.add_argument('samples');p.add_argument('--output',required=True)
    args=parser.parse_args()
    from .storage import resolve_output_path
    if getattr(args,'output',None) is not None:
        args.output=resolve_output_path(args.output)
    if args.command=='gui':
        from .gui import launch
        return launch(args.config)
    if args.command=='report':
        from .report import write_report
        print(write_report(args.folder).resolve());return
    if args.command=='analyze':
        from .analysis import analyze_scan
        args.output=resolve_output_path(args.output or Path(args.folder)/'analysis')
        options=json.loads(Path(args.options).read_text()) if args.options else None
        result=analyze_scan(args.folder,options,args.output,lambda phase,info:print(phase,info,flush=True))
        print(json.dumps({k:result[k] for k in ['candidate_count','rejected_count','distinct_images']},indent=2));return
    if args.command=='review':
        from .review import serve_review
        import threading
        server,url=serve_review(args.folder,args.port,not args.no_open)
        print(url,flush=True)
        try:threading.Event().wait()
        except KeyboardInterrupt:server.shutdown();server.server_close()
        return
    config=demo_config() if args.command=='demo' else load_config(args.config)
    if args.command=='identify':
        from .scan import identify_chips
        print(json.dumps([asdict(c) for c in identify_chips(args.folder,**config.get('grid',{}))],indent=2));return
    if args.command=='train':
        from .segmentation import train_contrast_model
        samples=json.loads(Path(args.samples).read_text())
        atomic_json(Path(args.output),train_contrast_model(samples,config['context'],config['context'].get('material','unknown')));return
    scope=connect(config)
    def progress(event,frame,info):
        if event!='focus': print(event,json.dumps(info),flush=True)
    try:
        if args.command=='status': print(asdict(scope.position()));return
        if args.command=='optics-status':
            controls=scope.hardware_controls();print(json.dumps({'objectives':controls.objectives(),'illumination_percent':controls.lamp_percent()},indent=2));return
        if args.command=='stop':
            errors=scope.stop();print('STOP requested.',errors);return 1 if errors else 0
        if args.command=='capture':
            from .imaging import save_frame
            save_frame(Path(args.output),'frame',scope.capture());return
        arm(scope,config)
        if args.command=='optimize-exposure':
            from .optics import optimize_exposure
            print(json.dumps(optimize_exposure(scope,args.output,args.initial_ms,args.white_level),indent=2));return
        if args.command=='objective-profile':
            from .calibration import calibrate_camera
            from .optics import save_objective_profile
            if args.use_existing_calibration:transform=transform_from_config(config)
            else:
                scope.expected_scale=None
                from .autofocus import autofocus
                result=autofocus(scope,**config['focus'])
                if not result.success:raise RuntimeError(result.reason)
                transform,quality=calibrate_camera(scope,config.get('calibration_step_um',20),Path(args.output).with_suffix('')/'calibration')
            print(json.dumps(save_objective_profile(scope,config,transform,args.output),indent=2));return
        if args.command=='revisit':
            from .revisit import run_revisits
            result=run_revisits(scope,args.folder,json.loads(Path(args.profile).read_text()),args.output,args.selection,args.max_candidates,args.resume,progress,config.get('analysis'))
            print(json.dumps({'state':result['state'],'verified_count':result['verified_count'],'output':str(Path(args.output).resolve())},indent=2));return
        if args.command=='focus':
            from .autofocus import autofocus,autofocus_range
            result=(autofocus_range(scope,**config['initial_focus'],output=args.output) if args.wide else autofocus(scope,**config['focus'],output=args.output))
            print(json.dumps(asdict(result),indent=2));return 0 if result.success else 2
        if args.command=='calibrate':
            from .calibration import calibrate_camera
            config['camera_transform']=None;atomic_json(Path(args.config),config)
            transform,quality=calibrate_camera(scope,config.get('calibration_step_um',20))
            save_transform(args.config,config,transform,quality);print(asdict(transform));return
        if args.command=='demo': return run_demo(scope,config,args.output,progress)
        from .scan import ScanPlan,run_scan
        from .workflow import scan_chips
        from .segmentation import ContrastModel
        from .report import write_report
        transform=transform_from_config(config)
        model=ContrastModel.load(config['material_model']) if config.get('material_model') else None
        if args.command=='survey': run_scan(scope,ScanPlan(**config['survey']),transform,args.output,context=config.get('context'),callback=progress,resume=args.resume)
        else: scan_chips(scope,transform,args.survey,args.output,config,model,progress,args.resume)
        print(write_report(args.output).resolve())
    except KeyboardInterrupt:
        errors=scope.stop();print('Stopped.',errors,file=sys.stderr);return 130
    finally: scope.close()

def run_demo(scope,config,output,callback=None):
    from .autofocus import autofocus_range
    from .calibration import calibrate_camera
    from .scan import ScanPlan,run_scan,identify_chips
    from .workflow import scan_chips
    from .report import write_report
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    scope.move_z(125)
    result=autofocus_range(scope,**config['initial_focus'],output=output/'initial-focus')
    if not result.success: raise RuntimeError(result.reason)
    transform,quality=calibrate_camera(scope)
    atomic_json(output/'camera-calibration.json',{'transform':asdict(transform),'quality':quality})
    run_scan(scope,ScanPlan(**config['survey']),transform,output/'survey',context=config['context'],callback=callback)
    chips=identify_chips(output/'survey',**config['grid'])
    scan_chips(scope,transform,output/'survey',output/'detail',config,callback=callback)
    write_report(output/'survey');write_report(output/'detail')
    atomic_json(output/'demo.json',{'source':'SIMULATION','state':'complete','chips':len(chips),
        'missing':json.loads((output/'survey/chips.json').read_text())['missing']})
    print('Demo complete:',output.resolve())

if __name__=='__main__':
    try: sys.exit(main() or 0)
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr);sys.exit(1)
