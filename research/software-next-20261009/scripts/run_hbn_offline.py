"""One explicit offline hBN segmentation/refinement invocation; no instrument imports.

Use fresh version names. STOP in the controller directory is forwarded to the
active scientific child; committed results and original acquisition remain.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

SCRIPTS=Path(__file__).resolve().parent


def read(path):
    for attempt in range(4):
        try:return json.loads(Path(path).read_bytes())
        except OSError as exc:
            if exc.errno not in (2,22) or attempt==3:raise
            time.sleep(.25*(attempt+1))


def digest(path):
    for attempt in range(4):
        try:
            result=hashlib.sha256()
            with Path(path).open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
            return result.hexdigest()
        except OSError as exc:
            if exc.errno not in (2,22) or attempt==3:raise
            time.sleep(.25*(attempt+1))


def canonical(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()


def save(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,separators=(',',':'),allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def inside(root,relative):
    path=(root/relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):raise ValueError('Output path escapes selected version')
    return path


def analysis_selection(project,run_id,chip,output,request_hash):
    """Return a selector only after one verifiable committed shard exists."""
    if not (output/'plan.json').is_file() or not (output/'ledger.json').is_file():return None
    plan=read(output/'plan.json');pin=plan['pin'];identity=pin['identity']
    request=read(project/'data'/run_id/'scan-request.json');settings=request['settings']
    if settings.get('material')!='hBN' or identity.get('run_id')!=run_id or identity.get('scan_request_sha256')!=request_hash:
        raise ValueError('Analysis differs from exact hBN request')
    if digest(project/'data'/run_id/'scan-request.json')!=request_hash or pin.get('chip')!=chip:raise ValueError('Request/chip changed')
    if canonical(pin['config'])!=pin['config_sha256']:raise ValueError('Analysis config changed')
    if digest(project/'data'/run_id/chip/'manifest.json')!=pin['manifest_sha256']:raise ValueError('Acquisition manifest changed')
    model=read(output/'model.json');context=model.get('applicability_context',model)
    if canonical(model)!=plan['model_sha256'] or context.get('material')!='hBN' or context.get('oxide_nm')!=settings.get('oxide_nm'):
        raise ValueError('Selected model differs from hBN/substrate context')
    ledger=read(output/'ledger.json')
    if not ledger:return None
    tile,commit=next(iter(ledger.items()))
    if tile not in pin['config']['tiles']:raise ValueError('Committed field outside pinned plan')
    relative=commit['field_json'];path=inside(output,relative)
    if digest(path)!=commit['hashes'].get(relative):raise ValueError('First committed field checksum mismatch')
    if read(path).get('field')!=chip+'/'+tile:raise ValueError('First committed field identity mismatch')
    return {'schema':1,'run_id':run_id,'analysis_relative_path':output.relative_to(project).as_posix(),
        'plan_sha256':digest(output/'plan.json')}


def review_selection(project,run_id,output,analysis):
    if not (output/'plan.json').is_file() or not (output/'latest.json').is_file():return None
    plan=read(output/'plan.json');plan_hash=digest(output/'plan.json');latest=read(output/'latest.json')
    if plan.get('run_id')!=run_id or plan.get('plan_sha256',plan.get('source_plan_sha256'))!=analysis['plan_sha256']:
        raise ValueError('Refinement differs from selected analysis')
    if latest.get('refinement_plan_sha256')!=plan_hash:raise ValueError('Refinement generation plan mismatch')
    index=inside(output,latest['index_path'])
    if digest(index)!=latest['sha256']:raise ValueError('Refinement generation checksum mismatch')
    value=read(index)
    if value.get('run_id')!=run_id or value.get('plan_sha256')!=analysis['plan_sha256'] or value.get('refinement_plan_sha256')!=plan_hash:
        raise ValueError('Refinement index source mismatch')
    return {'schema':1,'run_id':run_id,'plan_sha256':analysis['plan_sha256'],
        'refinement':{'kind':'stream','path':output.relative_to(project).as_posix(),'plan_sha256':plan_hash}}


def publish(run,control,name,chip,selector):
    if name=='analysis':
        # A prior refinement selector cannot be applied to a different plan.
        # Preserve its exact bytes as audit evidence before retiring the pointer.
        for filename in (f'hbn-review-selection-{chip}.json','hbn-review-selection.json'):
            target=run/filename
            if target.exists() and read(target).get('plan_sha256')!=selector['plan_sha256']:
                (control/('previous-'+filename)).write_bytes(target.read_bytes())
                target.unlink()
    for filename in (f'hbn-{name}-selection-{chip}.json',f'hbn-{name}-selection.json'):
        target=run/filename
        if target.exists():save(control/('previous-'+filename),read(target))
        save(target,selector)


def run(args):
    project=args.project.resolve();run_folder=project/'data'/args.run_id
    if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',args.run_id) or not re.fullmatch(r'chip\d+',args.chip):raise ValueError('Exact run/chip required')
    for version in (args.version,args.refinement_version):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',version):raise ValueError('Simple unique versions required')
    if args.version==args.refinement_version:raise ValueError('Segmentation and refinement require different versions')
    request=read(run_folder/'scan-request.json')
    if request.get('id')!=args.run_id or request.get('settings',{}).get('material')!='hBN':raise ValueError('Only the selected saved hBN run is supported')
    request_hash=digest(run_folder/'scan-request.json')
    model=args.model.resolve();model_hash=digest(model)
    base=inside(project,Path('analysis/hbn')/args.run_id/args.chip)
    analysis=base/args.version;refinement=base/args.refinement_version;control=base/(args.version+'-controller')
    if any(p.exists() for p in (analysis,refinement,control)):raise ValueError('Choose fresh output versions; existing results are preserved')
    control.mkdir(parents=True)
    state={'schema':1,'run_id':args.run_id,'chip':args.chip,'pid':os.getpid(),'state':'running','phase':'starting',
        'started_at':time.time(),'no_acquisition_performed':True,'model_sha256':model_hash,
        'scan_request_sha256':request_hash,'analysis_version':args.version,'refinement_version':args.refinement_version,
        'controller_sha256':digest(Path(__file__)),'workers':2}
    save(control/'invocation.json',{**state,'arguments':{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}})
    def status(**values):
        state.update(values,updated_at=time.time());save(control/'status.json',state)
    def child(stage,command,output,on_progress):
        if (control/'STOP').exists():raise InterruptedError('Offline workflow STOP before next stage')
        status(phase=stage,command=command)
        with (control/(stage+'.log')).open('ab',buffering=0) as log:
            process=subprocess.Popen(command,cwd=SCRIPTS.parent,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name=='nt' else 0)
            status(child_pid=process.pid)
            stopped=False;published=False
            try:
                while True:
                    if (control/'STOP').exists():
                        output.mkdir(parents=True,exist_ok=True);(output/'STOP').touch(exist_ok=True);stopped=True
                    if not stopped and not published:published=on_progress()
                    code=process.poll()
                    progress=read(output/'status.json') if (output/'status.json').is_file() else {}
                    status(child_returncode=code,child_progress={k:progress[k] for k in ('state','finished_fields','selected_fields','fields_complete','total_fields','regions_complete','reason','counts') if k in progress})
                    if code is not None:break
                    time.sleep(2)
            finally:
                if process.poll() is None:
                    output.mkdir(parents=True,exist_ok=True);(output/'STOP').touch(exist_ok=True)
                    # Cooperative child shutdown retains committed field records.
                    process.wait()
            if stopped or progress.get('state')=='stopped':raise InterruptedError('Offline workflow STOP; committed outputs retained')
            if code or progress.get('state')!='complete':raise RuntimeError(stage+' did not complete: '+str(progress.get('reason',code)))
            if not published and not on_progress():raise RuntimeError(stage+' completed without a publishable result')
    selected={}
    def publish_analysis():
        value=analysis_selection(project,args.run_id,args.chip,analysis,request_hash)
        if not value:return False
        selected.update(value)
        if not getattr(args,'defer_selection',False):
            publish(run_folder,control,'analysis',args.chip,value)
            status(analysis_published=True)
        status(analysis_plan_sha256=value['plan_sha256']);return True
    def publish_review():
        value=review_selection(project,args.run_id,refinement,selected)
        if not value:return False
        if getattr(args,'defer_selection',False):
            # Keep the prior reviewed pilot visible until screened full-run
            # results exist, rather than replace it with color-only proposals.
            publish(run_folder,control,'analysis',args.chip,selected)
            status(analysis_published=True)
        publish(run_folder,control,'review',args.chip,value);status(refinement_published=True);return True
    try:
        child('segmentation',[sys.executable,str(SCRIPTS/'analyze_graphene_run.py'),'--project',str(project),
            '--run-id',args.run_id,'--chip',args.chip,'--version',args.version,'--model',str(model),'--tiles','all','--workers','2'],analysis,publish_analysis)
        child('refinement',[sys.executable,str(SCRIPTS/'refine_graphene_regions.py'),'--analysis',str(analysis),
            '--version',args.refinement_version,'--tiles','all','--workers','2'],refinement,publish_review)
        status(state='complete',phase='complete',completed_at=time.time())
    except InterruptedError as exc:status(state='stopped',reason=str(exc))
    except BaseException as exc:status(state='failed',reason=str(exc));raise
    return state


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    for key in ('project','model'):result.add_argument('--'+key,type=Path,required=True)
    for key in ('run-id','chip','version','refinement-version'):result.add_argument('--'+key,required=True)
    result.add_argument('--defer-selection',action='store_true',help='Keep previous selected review until first refinement generation is available.')
    return result


if __name__=='__main__':print(json.dumps(run(parser().parse_args()),sort_keys=True))
