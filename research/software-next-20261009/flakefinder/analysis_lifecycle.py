"""Durable, offline-only scan-to-review jobs. Never starts acquisition."""
from contextlib import contextmanager
from pathlib import Path
import argparse
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid

RELEASE=Path(__file__).resolve().parents[1]
VERSION='scan-to-review-v1'
TERMINAL={'complete','complete_with_warnings','unsupported','failed','stopped','no_committed_fields','needs_attention'}
ACTIVE_SCAN={'queued','starting','running','preparing','discovering','scanning','reference_prepared'}


_SEEN_READS=set()


def retry_read(action,check=lambda:None):
    for attempt in range(5):
        check()
        try:return action()
        except OSError as exc:
            if exc.errno not in (errno.EINVAL,errno.ENOENT) or attempt==4:raise
            check();time.sleep(.05*2**attempt)


def read(path,default=None,*,check=lambda:None):
    path=Path(path)
    def action():
        try:
            value=json.loads(path.read_text(encoding='utf-8-sig'));_SEEN_READS.add(str(path));return value
        except FileNotFoundError:
            if str(path) in _SEEN_READS:raise
            return {} if default is None else default
    return retry_read(action,check)


def save(path,value,exclusive=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    data=json.dumps(value,sort_keys=True,allow_nan=False).encode()
    if exclusive:
        with path.open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        return
    temporary=path.with_name('.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    retry_read(lambda:os.replace(temporary,path))


def digest(path,check=lambda:None):
    def action():
        h=hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda:stream.read(1024**2),b''):check();h.update(block)
        return h.hexdigest()
    return retry_read(action,check)


def capability(settings):
    supported=settings.get('material')=='graphene' and settings.get('oxide_nm')==90 and settings.get('substrate','SiO2/Si')=='SiO2/Si'
    return {'supported':supported,'pipeline':VERSION if supported else None,
        'reason':'Conditional graphene optical assessment, boundary review and spatial grouping; no confirmed material or layer assignment.' if supported else
        'Automatic analysis requires a supported material/substrate model. MoTe2 historical review and explicit profile-based analysis remain available; hBN is not automatically classified.',
        'material':settings.get('material'),'substrate':settings.get('substrate','SiO2/Si'),'oxide_nm':settings.get('oxide_nm')}


def folder(project,run_id):
    root=Path(project).resolve()
    if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',run_id):raise ValueError('Exact scan run required')
    run=(root/'data'/run_id).resolve()
    if run.parent!=(root/'data').resolve():raise ValueError('Redirected scan directory')
    return run


def source_files():
    scripts=('analyze_graphene_run.py','watch_graphene_scan.py','refine_graphene_regions.py','group_saved_graphene.py')
    modules=('analysis_lifecycle.py','material_input.py','material_terraces.py','material_contrast.py','material_profiles.py',
        'mote2_optics.py','terrace_domain.py','terrace_analysis.py','imaging.py','model.py','graphene_useful_regions.py',
        'graphene_refinement.py','graphene_dedup.py','graphene_review.py')
    paths=[*(RELEASE/'scripts'/name for name in scripts),*(RELEASE/'flakefinder'/name for name in modules),
        RELEASE/'configs/materials/graphene-sio2-90nm.json',*sorted((RELEASE/'configs/materials/data').glob('*'))]
    return {p.relative_to(RELEASE).as_posix():digest(p) for p in paths if p.is_file()}


def enqueue(project,run_id):
    run=folder(project,run_id);request=read(run/'scan-request.json')
    if request.get('id')!=run_id:raise ValueError('Request identity mismatch')
    if request['settings'].get('analysis_after_scan') is not True:return {'state':'not_requested'}
    job=run/'offline-workflow';pin=digest(run/'scan-request.json');supported=capability(request['settings'])
    previous=read(job/'plan.json')
    if previous:
        if previous.get('request_sha256')!=pin:raise ValueError('Queued request changed')
        return snapshot(project,run_id)
    plan={'schema':1,'version':VERSION,'run_id':run_id,'request_sha256':pin,'created_at':time.time(),
        'capability':supported,'source_sha256':source_files(),'versions':{'analysis':'auto-segmentation-v1','refinement':'auto-boundaries-v1','dedup':'auto-review-v1'},
        'policy':{'factor':2,'minimum_free_mib':768,'native_threads':1,'field_timeout_s':600.,'minimum_review_area_um2':25.},
        'no_acquisition_actions':True}
    save(job/'plan.json',plan,exclusive=True)
    save(job/'status.json',{'state':'queued' if supported['supported'] else 'unsupported','reason':supported['reason'],
        'run_id':run_id,'updated_at':time.time(),'chips':{},'no_acquisition_actions':True})
    return snapshot(project,run_id)


def process_alive(pid):
    """Fail closed when process absence cannot be established."""
    if type(pid) is not int or pid<=0:return False
    if os.name=='nt':
        import ctypes
        api=ctypes.WinDLL('kernel32',use_last_error=True);api.OpenProcess.restype=ctypes.c_void_p
        api.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
        api.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)];api.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=api.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()!=87
        try:
            code=ctypes.c_ulong()
            return not api.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:api.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


@contextmanager
def owner_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a+b');stream.write(b'0');stream.flush();stream.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:stream.close()


def snapshot(project,run_id):
    run=folder(project,run_id);job=run/'offline-workflow';plan=read(job/'plan.json');state=read(job/'status.json')
    if not plan:return {'state':'not_requested','chips':{}}
    if digest(run/'scan-request.json')!=plan['request_sha256']:return {'state':'failed','reason':'Queued request checksum mismatch','chips':{}}
    launch=read(job/'launch.json')
    return {**state,'worker_alive':process_alive(launch.get('pid')),'capability':plan['capability'],
        'acquisition':read(run/'scan-status.json'),
        'run_id':run_id,'no_acquisition_actions':True}


def ensure_worker(project,run_id,*,launcher=None):
    run=folder(project,run_id);job=run/'offline-workflow';state=snapshot(project,run_id)
    if state['state'] in TERMINAL or state['state']=='not_requested' or (job/'STOP').exists():return state
    with owner_lock(job/'launch.lock'):
        launch=read(job/'launch.json')
        if process_alive(launch.get('pid')):return snapshot(project,run_id)
        args=[sys.executable,'-m','flakefinder.analysis_lifecycle','--project',str(Path(project).resolve()),'--run',run_id]
        env=os.environ.copy()
        for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):env[key]='1'
        flags=(subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name=='nt' else 0
        with (job/'worker.log').open('ab') as log:
            child=(launcher or subprocess.Popen)(args,cwd=str(RELEASE),env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=flags)
        save(job/'launch.json',{'pid':child.pid,'args':args,'started_at':time.time()})
    return snapshot(project,run_id)


def recover_queued(project):
    """Viewer startup may recover explicitly queued offline jobs only."""
    results=[]
    for plan in (Path(project)/'data').glob('scan-*/offline-workflow/plan.json'):
        run_id=plan.parent.parent.name
        try:results.append(ensure_worker(project,run_id))
        except (OSError,ValueError) as exc:results.append({'run_id':run_id,'state':'recovery_error','reason':str(exc)})
    return results


def acquisition_closed(run,plan,alive=process_alive):
    if digest(run/'scan-request.json')!=plan['request_sha256']:raise ValueError('Queued request changed')
    status=read(run/'scan-status.json');receipt=read(run/'worker-start.json');launch=read(run/'launch.json')
    if status.get('hardware_closed') is not True or status.get('phase')!='finished':return False
    if receipt and receipt.get('request_sha256')!=plan['request_sha256']:raise ValueError('Acquisition receipt request mismatch')
    return not any(alive(pid) for pid in (status.get('pid'),receipt.get('pid'),launch.get('pid')) if pid)


def pin_inputs(run,job):
    files={}
    for path in sorted(run.glob('chip*/manifest.json')):
        if not re.fullmatch(r'chip\d+',path.parent.name) or path.resolve()!=path:raise ValueError('Invalid chip source')
        manifest=read(path)
        if manifest.get('completed'):files[path.parent.name]={'manifest_sha256':digest(path),'fields':len(manifest['completed'])}
    result={'chips':files,'readonly_sources':read(run/'batch.json').get('completed_chip_sources',{})}
    previous=read(job/'inputs.json')
    if previous and previous!=result:raise ValueError('Closed acquisition manifests changed')
    if not previous:save(job/'inputs.json',result,exclusive=True)
    return result


def readonly_reviews(project,run,inputs):
    """Expose inherited scientific owners explicitly; never relabel donor data."""
    result={}
    if not inputs['readonly_sources']:return result
    from .completed_artifacts import resolve_completed_chip
    from .graphene_review import GrapheneReview
    reader=GrapheneReview(project);batch=read(run/'batch.json')
    for chip in inputs['readonly_sources']:
        try:
            source=resolve_completed_chip(run,batch,chip)
            owner,request,pin=reader._run(source.parent.name)
            selected=reader._offline(owner,owner.name,pin,chip)
            if selected is None:raise ValueError('No explicit analysis selected for this inherited chip')
            result[chip]={'state':'review_available','source_run_id':owner.name,'review_url':'/graphene?run='+owner.name+'&chip='+chip,
                'analysis_state':selected[1]['state'],'relationship':'Pinned read-only acquisition donor; no new acquisition or copied scientific identity.'}
        except (OSError,ValueError,KeyError,RuntimeError) as exc:
            result[chip]={'state':'needs_analysis_selection','reason':str(exc),'source_run_id':inputs['readonly_sources'][chip].get('source_run_id')}
    return result


def _publish(project,run,chip,analysis,refinement=None,dedup=None):
    plan=read(analysis/'plan.json');sha=digest(analysis/'plan.json')
    if (plan['pin']['identity']['run_id']!=run.name or plan['pin']['chip']!=chip or plan['pin']['identity'].get('scan_request_sha256')!=digest(run/'scan-request.json') or plan['pin'].get('manifest_sha256')!=digest(run/chip/'manifest.json')):raise ValueError('Analysis output identity mismatch')
    selection={'schema':1,'run_id':run.name,'analysis_relative_path':analysis.relative_to(project).as_posix(),'plan_sha256':sha}
    selection_path=run/f'graphene-analysis-selection-{chip}.json'
    previous=read(selection_path)
    if previous and previous!=selection:raise ValueError('Explicit per-chip analysis selection changed')
    save(selection_path,selection)
    if not (run/'graphene-analysis-selection.json').exists():save(run/'graphene-analysis-selection.json',selection,exclusive=True)
    review={'schema':1,'run_id':run.name,'plan_sha256':sha}
    if refinement:
        review['refinement']={'kind':'stream','path':refinement.relative_to(project).as_posix(),'plan_sha256':digest(refinement/'plan.json')}
    if dedup:review['dedup']={'path':(dedup/'dedup.json').relative_to(project).as_posix(),'sha256':digest(dedup/'dedup.json')}
    if len(review)>3:
        review_path=run/f'graphene-review-selection-{chip}.json';previous=read(review_path)
        if previous:
            if any(previous.get(key)!=review[key] for key in ('schema','run_id','plan_sha256')) or any(key in previous and previous[key]!=review[key] for key in ('refinement','dedup') if key in review):raise ValueError('Explicit per-chip review selection changed')
            review={**previous,**review}
        save(review_path,review)
        if read(run/'graphene-analysis-selection.json')==selection:save(run/'graphene-review-selection.json',review)


def stage_command(project,run,chip,plan,stage):
    base=project/'analysis/graphene'/run.name/chip;versions=plan['versions'];policy=plan['policy']
    if stage=='analysis':
        output=base/versions[stage]
        args=['analyze_graphene_run.py','--project',str(project),'--run-id',run.name,'--chip',chip,'--version',versions[stage],
            '--factor',str(policy['factor']),'--field-timeout',str(policy['field_timeout_s']),'--min-free-mib',str(policy['minimum_free_mib'])]
        status=output/'status.json';stop=output/'STOP'
    elif stage=='refinement':
        output=base/versions[stage];args=['refine_graphene_regions.py','--analysis',str(base/versions['analysis']),'--version',versions[stage]]
        status=output/'status.json';stop=output/'STOP'
    else:
        output=base/versions[stage];args=['group_saved_graphene.py','--project',str(project),'--run-id',run.name,'--chip',chip,
            '--version',versions[stage],'--minimum-area',str(policy['minimum_review_area_um2'])]
        status=output/'dedup-progress.json';stop=output/'DEDUP_STOP'
    return [sys.executable,str(RELEASE/'scripts'/args[0]),*args[1:]],output,status,stop


def run_job(project,run_id,*,alive=process_alive,sleep=time.sleep,popen=None):
    project=Path(project).resolve();run=folder(project,run_id);job=run/'offline-workflow';plan=read(job/'plan.json')
    if not plan or not plan['capability']['supported']:raise ValueError('Supported queued offline job required')
    state=read(job/'status.json');state.update(pid=os.getpid(),run_id=run_id,no_acquisition_actions=True)
    def update(**values):
        state.update(values,updated_at=time.time());save(job/'status.json',state)
    def check():
        if (job/'STOP').exists():raise InterruptedError('Offline workflow STOP requested; acquisition unchanged')
        if digest(run/'scan-request.json')!=plan['request_sha256']:raise ValueError('Queued scan request changed')
    with owner_lock(job/'owner.lock'):
        try:
            while not acquisition_closed(run,plan,alive):
                check();update(state='waiting_for_acquisition',phase='waiting_for_closed_owner');sleep(2)
            check()
            previous_child=read(job/'child.json')
            while alive(previous_child.get('pid')):
                if (job/'STOP').exists() and previous_child.get('stage') in ('analysis','refinement','dedup') and re.fullmatch(r'chip\d+',previous_child.get('chip','')):
                    _,_,_,prior_stop=stage_command(project,run,previous_child['chip'],plan,previous_child['stage'])
                    if not prior_stop.exists():save(prior_stop,{'reason':'Offline lifecycle STOP after recovery','at':time.time()},exclusive=True)
                check();update(state='waiting_for_existing_stage',phase=previous_child.get('stage'));sleep(2)
            for relative,expected in plan['source_sha256'].items():
                if digest(RELEASE/relative)!=expected:raise ValueError('Pinned offline software changed: '+relative)
            inputs=pin_inputs(run,job);inherited=readonly_reviews(project,run,inputs)
            inherited_attention=any(v['state']!='review_available' for v in inherited.values())
            update(readonly_reviews=inherited)
            if not inputs['chips']:
                update(state='needs_attention' if inherited_attention else ('complete' if inherited else 'no_committed_fields'),phase='finished');return state
            # One memory-heavy postprocessing owner across all queued scans.
            while True:
                check()
                try:
                    global_lock=owner_lock(project/'analysis/offline-workflow.lock');global_lock.__enter__();break
                except OSError:update(state='waiting_for_analysis_slot');sleep(5)
            try:
                for chip in sorted(inputs['chips']):
                    item=state.setdefault('chips',{}).setdefault(chip,{})
                    for stage in ('analysis','refinement','dedup'):
                        check();command,output,status_path,stop_path=stage_command(project,run,chip,plan,stage)
                        if stop_path.exists():raise InterruptedError('Independent '+stage+' STOP is retained')
                        child_status=read(status_path)
                        while child_status.get('state')!='complete':
                            check()
                            from .acquisition_resources import available_memory_bytes
                            if available_memory_bytes()<plan['policy']['minimum_free_mib']*1024**2:
                                update(state='resource_paused',phase=stage,chip=chip,reason='Waiting for offline RAM reserve');sleep(10);continue
                            update(state='running',phase=stage,chip=chip,reason=None)
                            env=os.environ.copy()
                            for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):env[key]='1'
                            flags=(subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS) if os.name=='nt' else 0
                            with (job/(chip+'-'+stage+'.log')).open('ab') as log:
                                child=(popen or subprocess.Popen)(command,cwd=str(RELEASE),env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=flags)
                            save(job/'child.json',{'pid':child.pid,'command':command,'chip':chip,'stage':stage,'started_at':time.time()})
                            while child.poll() is None:
                                if (job/'STOP').exists() and not stop_path.exists():save(stop_path,{'reason':'Offline lifecycle STOP','at':time.time()},exclusive=True)
                                update(child=read(status_path),child_pid=child.pid);sleep(2)
                            check();child_status=read(status_path)
                            if child_status.get('state')=='resource_paused':sleep(10);continue
                            if child.returncode or child_status.get('state')!='complete':raise RuntimeError(stage+' did not complete: '+str(child_status.get('reason',child_status.get('state'))))
                        item[stage]={'state':'complete','output':output.relative_to(project).as_posix(),'status':child_status}
                        analysis=project/item['analysis']['output']
                        _publish(project,run,chip,analysis,project/item['refinement']['output'] if 'refinement' in item else None,
                            project/item['dedup']['output'] if 'dedup' in item else None)
                        item['review_url']='/graphene?run='+run_id+'&chip='+chip
                        update(chips=state['chips'],child=None,child_pid=None)
                warnings=any(any(count for name,count in v.get('status',{}).get('counts',{}).items() if name!='assessed') or v.get('status',{}).get('failed_fields',0) or v.get('status',{}).get('unavailable_fields',0)
                    for c in state['chips'].values() for v in c.values() if isinstance(v,dict))
                update(state='needs_attention' if inherited_attention else ('complete_with_warnings' if warnings else 'complete'),phase='finished',readonly_reviews=inherited)
            finally:global_lock.__exit__(None,None,None)
        except InterruptedError as exc:update(state='stopped',reason=str(exc))
        except Exception as exc:update(state='failed',reason=str(exc),error_type=type(exc).__name__)
    return state


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--project',type=Path,required=True);p.add_argument('--run',required=True)
    args=p.parse_args();result=run_job(args.project,args.run);print(json.dumps(result));return int(result['state']=='failed')


if __name__=='__main__':raise SystemExit(main())
