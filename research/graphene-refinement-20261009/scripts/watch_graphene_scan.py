"""Incremental, read-only acquisition consumer; never imports an instrument client.

Only manifest-committed raw tiles from one pinned scan request are assessed.
Outputs, ledger and STOP are independent of acquisition. No raw files are deleted.
"""
from pathlib import Path
import argparse,gc,hashlib,json,os,re,shutil,sys,time,uuid

for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='2'
WORKSPACE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORKSPACE))
VERSION='graphene-committed-terraces-v2-interior'
TERMINAL={'complete','complete_with_boundary_limits','search_sampling_complete','chip_budget_reached',
          'time_budget_reached','storage_budget_reached','stopped','failed','failed_closed','focus_unresolved'}
QUALITY_MESSAGES={
    'No sufficient independently recorded bare-substrate support; no color measurement or zero-contrast pass.',
    'Insufficient independent substrate samples.','Insufficient retained substrate samples.',
    'Substrate support cannot sustain spatial held-out fit validation.',
    'At least100 independent recorded substrate pixels required; no inferred zero-background pass.',
    'Insufficient clear substrate support at the configured smoothing scale.'}


class AnalysisStop(Exception):pass
class FieldBudget(Exception):pass
class ResourcesPaused(Exception):pass
class MemoryPaused(ResourcesPaused):pass


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024**2),b''):h.update(block)
    return h.hexdigest()


def canonical(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path,value):
    from flakefinder.terrace_analysis import _plain
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.j'+uuid.uuid4().hex)
    with temp.open('x',encoding='utf-8') as stream:
        json.dump(_plain(value),stream,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    for attempt in range(7):
        try:os.replace(temp,path);break
        except PermissionError:
            if attempt==6:raise
            time.sleep(min(.05*2**attempt,.5))


def check_resources(output):
    from flakefinder.acquisition_resources import available_memory_bytes
    from flakefinder.storage import verify_project
    verify_project(acquisition=True)  # Storage/cache check only; no hardware/hold mutation.
    if shutil.disk_usage(output).free<10*1024**3+256*1024**2:
        raise ResourcesPaused('Output storage cannot retain10GiB reserve plus256MiB derived-field allowance.')
    if available_memory_bytes()<4*1024**3:
        raise MemoryPaused('Analysis waits for4GiB available host RAM, including2GiB analysis allowance; scanner has priority.')


def request_identity(project,run_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}',run_id):raise ValueError('Exact simple run ID required.')
    project=Path(project).resolve(strict=True);run=(project/'data'/run_id).resolve(strict=True)
    if run.parent!=(project/'data').resolve():raise ValueError('Run redirects outside the exact project data directory.')
    marker=read(project/'.flakefinder-project.json');request=read(run/'scan-request.json')
    if not (marker.get('project_id') or marker.get('id')):raise ValueError('Managed project identity is absent.')
    if request.get('id')!=run_id or request.get('settings',{}).get('material')!='graphene' or request['settings'].get('oxide_nm')!=90:
        raise ValueError('Watcher requires this exact graphene90nm scan request.')
    return run,{'project_id':marker.get('project_id',marker.get('id')),'run_id':run_id,
                'scan_request_sha256':digest(run/'scan-request.json')}


def committed_fields(run):
    """Never glob TIFFs as work: only durable completed records become input."""
    result=[]
    for path in sorted(run.glob('chip*/manifest.json')):
        manifest=read(path)
        for tile,record in sorted(manifest.get('completed',{}).items()):
            if not re.fullmatch(r'tile_[0-9]+',tile):raise ValueError('Unexpected committed tile identity.')
            hashes=record.get('hashes',{});required=['tiles/'+tile+'.tiff','tiles/'+tile+'.json','boundary-labels/'+tile+'.npz']
            if any(not hashes.get(key) for key in required):raise ValueError('Committed tile lacks raw/sidecar/background hashes.')
            result.append({'field':path.parent.name+'/'+tile,'chip':path.parent,'tile':tile,'record':record,
                'record_sha256':canonical(record),'source_hashes':{key:hashes[key] for key in required},
                'transform':manifest['transform'],'camera_shape':manifest['camera_shape'],
                'geometry_sha256':canonical({key:manifest.get(key) for key in ('transform','camera_shape','context','plan')}),
                'context':manifest.get('context',{}),'acquisition_settings':manifest.get('acquisition_settings',{})})
    return result


def verify_source(entry):
    for relative,expected in entry['source_hashes'].items():
        path=(entry['chip']/relative).resolve()
        if not path.is_relative_to(entry['chip'].resolve()) or not path.is_file() or digest(path)!=expected:
            raise ValueError('Committed acquisition source changed: '+relative)
    manifest=read(entry['chip']/'manifest.json');current=manifest['completed'].get(entry['tile'])
    if canonical(current)!=entry['record_sha256']:raise ValueError('Committed acquisition record changed.')
    if canonical({key:manifest.get(key) for key in ('transform','camera_shape','context','plan')})!=entry['geometry_sha256']:
        raise ValueError('Source geometry/context changed during analysis.')


def input_context(entry,run_id):
    if entry['context'].get('material')!='graphene' or entry['context'].get('oxide_nm')!=90:
        raise ValueError('Committed chip material/oxide context differs from graphene90nm.')
    sidecar=read(entry['chip']/'tiles'/(entry['tile']+'.json'));metadata=sidecar['metadata']
    objective=metadata.get('objective',{})
    if objective.get('magnification')!=20 or objective.get('na')!=.4 or metadata.get('white_level')!=4095:
        raise ValueError('Unsupported raw optics: expected measured20x NA0.4 white4095.')
    profile={key:metadata.get(key) for key in ('camera_job_template','camera_resources','objective','illumination_percent','white_level')}
    return {'material':'graphene','oxide_nm':90.,'optical_profile_id':run_id+'-'+canonical(profile)[:16],
        'radiometry':{'raw_linear_status':'assumed_unverified','dark_offset_status':'unmeasured_explicit_zero',
            'dark_offset_rgb':[0.,0.,0.],
            'response_assumption':'Manufacturer typical sensor response with finite unmeasured illumination/NA/oxide scenarios; not local calibration.'}}


def prepare_model(output,model_path=None):
    if model_path:
        model=read(model_path)
        if model.get('material')!='graphene' or model.get('oxide_nm')!=90 or 'contrast_rgb' not in model:
            raise ValueError('Supply a graphene90 conditional RGB model, not a spectral-only or foreign labeled fit.')
        return model
    from flakefinder.material_profiles import load_material_profile,manufacturer_response_scenarios,predict_material_ensemble
    profile=load_material_profile(WORKSPACE/'configs/materials/graphene-sio2-90nm.json')
    response=WORKSPACE/'configs/materials/data/Axiocam305_vendor_response.json'
    # Conventional optical-thickness grid, never asserted layer labels.
    thickness=[0.,.34,.68,1.02,1.36,1.7,2.04,2.72,3.4,5.1,6.8,10.2,20.4]
    wavelengths=read(response)['wavelengths_nm'][::2]
    model=predict_material_ensemble(profile,thickness,manufacturer_response_scenarios(response,temperatures_k=(3000.,6500.)),
        oxide_nm=(85.,90.,95.),illumination_na=(0.,.4),gamma=(1.,),wavelengths_nm=wavelengths)
    model['oxide_nm']=90.;model['nominal_oxide_nm']=90.
    model['watcher_assumptions']={'oxide_scenarios_nm':[85.,90.,95.],'gamma':1.,'gamma_measured_for_this_run':False,
        'no_layer_assignment':True,'vendor_response_sha256':digest(response),'unmeasured_dark_offset_rgb':[0.,0.,0.],
        'model_covariance_rgb_diagonal':.01**2,'model_covariance_kind':'Explicit1% engineering mismatch allowance; not a measured uncertainty or validated threshold.'}
    return model


def analyze_field(entry,context,classifier,attempt,check):
    import numpy as np
    from flakefinder.material_input import prepare_material_input
    from flakefinder.material_terraces import segment_material_terraces,MaterialTerraceOptions
    from flakefinder.model import CameraTransform,Position
    check();verify_source(entry)
    arrays,provenance=prepare_material_input(chip_folder=entry['chip'],tile_id=entry['tile'],context=context,maximum_pixels=6_000_000)
    check()
    result=segment_material_terraces(arrays['contrast_rgb'],arrays['valid_mask'],arrays['substrate_mask'],arrays['noise_covariance'],
        arrays['pixel_size_um'],classifier,saturated_mask=arrays['saturated_mask'],
        options=MaterialTerraceOptions(useful_area_um2=25.),check_cancel=check)
    check();verify_source(entry)
    transform=CameraTransform(**entry['transform']);position=Position(**entry['record']['position'])
    h,w=arrays['valid_mask'].shape;rows=[]
    for region in result['regions']:
        polygon=region['polygon_px'];rectangle=region.get('usable_rectangle',{})
        points=np.asarray(polygon);low=points.min(axis=0);high=points.max(axis=0)
        if rectangle.get('available') and rectangle.get('area_um2',0)>0:
            x,y,rw,rh=rectangle['bbox_px'];xy=np.array([x+(rw-1)/2,y+(rh-1)/2])
        else:
            yy,xx=np.nonzero(result['labels']==region['instance_label']);middle=len(xx)//2;xy=np.array([xx[middle],yy[middle]])
        stage=np.asarray(transform.matrix)@(xy-[(w-1)/2,(h-1)/2])+[position.x,position.y]
        selected=bool(region['selected_for_review']);useful=not 'below_useful_size' in region['filter_reasons']
        rows.append({**region,'id':entry['field']+'/'+str(region['instance_label']),'field':entry['field'],
            'source_tiff_sha256':entry['source_hashes']['tiles/'+entry['tile']+'.tiff'],
            'source_shape_hw':[h,w],'preview':entry['field'].split('/')[0]+'/tiles/'+entry['tile']+'.jpg',
            'polygons_px':[polygon,*region['holes_px']],'stage_xy_um':stage.tolist(),'source_position_um':entry['record']['position'],
            'source_transform_sha256':canonical(entry['transform']),'focus_evidence':entry['record'].get('full_chip_focus'),
            'bbox_px':[int(low[0]),int(low[1]),int(high[0]-low[0]+1),int(high[1]-low[1]+1)],
            'revisit_source_pixel_xy':xy.tolist(),'revisit_point_inside_observed_region':True,
            'disposition':'shortlist' if selected and useful else 'uncertain','rank':float(region['area_um2']*region['material_pixel_fraction']),
            'layer_number':None,'class_name':'conditional optical population; unconfirmed',
            'revisit_status':'candidate_coordinate_only; require unchanged coordinate frame and manual objective calibration'} )
    attempt.mkdir(parents=True,exist_ok=False)
    mask=attempt/'masks.npz';temporary=attempt/('.m'+uuid.uuid4().hex+'.npz')
    # Includes independent source proposals, unmatched regions and background controls.
    with temporary.open('xb') as stream:
        np.savez_compressed(stream,**{key:result[key] for key in ('labels','parent_labels','class_map','material_compatible_mask','debris_compatible_mask')},
            valid_mask=arrays['valid_mask'],substrate_mask=arrays['substrate_mask'],saturated_mask=arrays['saturated_mask'])
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,mask)
    counts={'shortlist':sum(r['disposition']=='shortlist' for r in rows),'uncertain':sum(r['disposition']=='uncertain' for r in rows)}
    payload={'field':entry['field'],'rows':rows,'report':result['report'],'prepared_input':provenance,'counts':counts,
        'source_record_sha256':entry['record_sha256'],'source_hashes':entry['source_hashes'],'source_transform':entry['transform'],
        'mask_sha256':digest(mask),'background_control':{'valid_pixels':provenance['valid_pixels'],'substrate_pixels':provenance['substrate_pixels'],
            'raw_noise_covariance':arrays['noise_covariance'].tolist()},'material_identity_confirmed':False,'layer_assignment_allowed':False}
    save(attempt/'field.json',payload);check();verify_source(entry)
    return payload



def background_priority():
    """Use pointer-sized Windows handles; a ctypes default int can truncate one."""
    if os.name!='nt':return 'not_applicable'
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.argtypes=[]
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.SetPriorityClass.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.SetPriorityClass.restype=wintypes.BOOL
    if not kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x4000):
        raise ctypes.WinError(ctypes.get_last_error())
    return 'below_normal'


def run(args):
    import cv2,numpy as np
    from threadpoolctl import threadpool_limits
    from flakefinder.material_contrast import MaterialContrastClassifier
    from flakefinder.terrace_domain import DomainExclusion
    from flakefinder.storage import resolve_output_path
    cv2.setNumThreads(2);threadpool_limits(2)
    run_folder,identity=request_identity(args.project,args.run_id)
    output=resolve_output_path(run_folder/'analysis-graphene-incremental');output.mkdir(parents=True,exist_ok=True)
    import msvcrt
    lock=(output/'analysis-owner.lock').open('a+b');lock.write(b'0');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:lock.close();raise RuntimeError('Another analysis worker owns this exact run.')
    source_files=[Path(__file__),*[WORKSPACE/'flakefinder'/name for name in ('material_input.py','material_terraces.py','material_contrast.py',
        'material_profiles.py','mote2_optics.py','terrace_domain.py','terrace_analysis.py','imaging.py','model.py')],
        WORKSPACE/'configs/materials/graphene-sio2-90nm.json',*sorted((WORKSPACE/'configs/materials/data').glob('*'))]
    source_hashes={str(p):digest(p) for p in source_files};plan_path=output/'plan.json';ledger={};catalog_rows=[];seen=set();started=time.time()
    state={**identity,'state':'starting','pid':os.getpid(),'processed_fields':0,'available_fields':0,'total_fields':None,
        'counts':{},'unassessable_fields':0,'failed_fields':0,'started_at':started,'updated_at':started,'no_acquisition_performed':True}
    def status():
        state.update(updated_at=time.time(),elapsed_seconds=time.time()-started);save(output/'status.json',state)
        ranked=sorted(catalog_rows,key=lambda r:r['rank'],reverse=True)[:1000]
        save(output/'catalog.json',{**state,'rows':ranked,'catalog_limit':1000,'full_observations':'All per-field field.json files retained.',
            'coordinate_frame_id':state.get('coordinate_frame_id'),'layer_assignment_allowed':False,'chemical_identity_confirmed':False})
    def check():
        if (output/'STOP').exists():raise AnalysisStop('Analysis-only STOP requested.')
        if digest(run_folder/'scan-request.json')!=identity['scan_request_sha256']:raise ValueError('Pinned scan request changed.')
    def wait_resources():
        waiting=time.monotonic()
        while True:
            check()
            try:check_resources(output);return
            except MemoryPaused as exc:
                state.update(state='waiting_for_ram',error=str(exc));status()
                if args.once or time.monotonic()-waiting>7200:raise
                for _ in range(15):time.sleep(1);check()
    try:
        state['process_priority']=background_priority()
        wait_resources()
        if plan_path.exists():
            plan=read(plan_path)
            if plan['identity']!=identity or plan['source_hashes']!=source_hashes:raise ValueError('Analysis source/request changed; use a new reviewed analysis version.')
            model=read(output/'model.json')
            if canonical(model)!=plan['model_sha256']:raise ValueError('Saved optical model changed.')
            if args.model and canonical(read(args.model))!=plan['model_sha256']:raise ValueError('Requested model differs from the existing analysis plan.')
        else:
            model=prepare_model(output,args.model);save(output/'model.json',model)
            plan={'version':VERSION,'identity':identity,'source_hashes':source_hashes,'model_sha256':canonical(model),
                  'field_timeout_s':args.field_timeout,'maximum_pixels':6_000_000,'threads':2,'raw_source_deletion':False}
            save(plan_path,plan)
        ledger=read(output/'ledger.json') if (output/'ledger.json').exists() else {}
        state['field_timeout_s']=plan['field_timeout_s'];state['state']='running';status()
        while True:
            check();batch=read(run_folder/'batch.json') if (run_folder/'batch.json').exists() else {}
            coordinate=batch.get('coordinate_frame_id')
            if plan.get('coordinate_frame_id') is not None and coordinate!=plan['coordinate_frame_id']:
                raise ValueError('Campaign coordinate frame differs from pinned analysis plan.')
            if coordinate is not None and plan.get('coordinate_frame_id') is None:
                plan['coordinate_frame_id']=coordinate;save(plan_path,plan)
            state['coordinate_frame_id']=coordinate
            entries=committed_fields(run_folder);state['available_fields']=len(entries);worked=False
            for entry in entries:
                key=entry['field']
                if key in seen:continue
                check();state['current_field']=key
                if key in ledger:
                    commit=ledger[key]
                    if commit['source_record_sha256']!=entry['record_sha256']:raise ValueError('Previously analyzed acquisition record changed.')
                    if commit['geometry_sha256']!=entry['geometry_sha256']:raise ValueError('Previously analyzed source geometry changed.')
                    for relative,h in commit['hashes'].items():
                        if digest(output/relative)!=h:raise ValueError('Committed analysis output changed.')
                    payload=read(output/commit['field_json'])
                else:
                    wait_resources();state.update(state='running');state.pop('error',None)
                    for p,h in source_hashes.items():
                        if digest(p)!=h:raise ValueError('Analysis implementation changed while running.')
                    context=input_context(entry,args.run_id);classifier=MaterialContrastClassifier(model,context=context,model_covariance=np.eye(3)*.01**2)
                    field_started=time.monotonic();last_health_check=0.;attempt=output/'fields'/(key.replace('/','__')+'-'+uuid.uuid4().hex[:8]);status()
                    def field_check():
                        nonlocal last_health_check
                        now=time.monotonic()
                        if now-field_started>plan['field_timeout_s']:raise FieldBudget('Field analysis time budget exceeded; partial output retained.')
                        # At most one health/read-only source-request check per
                        # second; avoid thousands of Drive metadata opens in a
                        # component loop. Native phases remain cooperative.
                        if now-last_health_check>=1:
                            check();last_health_check=now
                            from flakefinder.acquisition_resources import available_memory_bytes
                            if available_memory_bytes()<int(2.5*1024**3):
                                raise MemoryPaused('Analysis aborted uncommitted field to preserve2.5GiB available RAM for acquisition.')
                    try:
                        payload=analyze_field(entry,context,classifier,attempt,field_check)
                    except (AnalysisStop,ResourcesPaused):raise
                    except Exception as exc:
                        quality=isinstance(exc,DomainExclusion) or (type(exc).__module__ in ('flakefinder.model','builtins') and str(exc) in QUALITY_MESSAGES)
                        if not quality and not isinstance(exc,FieldBudget):raise
                        verify_source(entry);attempt.mkdir(parents=True,exist_ok=True)
                        payload={'field':key,'rows':[],'counts':{},'assessment_state':'unassessable' if quality else 'analysis_budget_exhausted',
                            'reason':str(exc),'source_features_not_disproved':True,'source_hashes':entry['source_hashes']}
                        save(attempt/'field.json',payload)
                    check();relative=(attempt/'field.json').relative_to(output).as_posix()
                    commit={'field_json':relative,'source_record_sha256':entry['record_sha256'],'geometry_sha256':entry['geometry_sha256'],'committed_at':time.time(),
                        'hashes':{p.relative_to(output).as_posix():digest(p) for p in attempt.iterdir() if p.is_file()},'counts':payload['counts']}
                    ledger[key]=commit;save(output/'ledger.json',ledger);worked=True;gc.collect()
                seen.add(key);state['processed_fields']+=1
                if payload.get('assessment_state'):state['unassessable_fields']+=1
                for name,value in payload.get('counts',{}).items():state['counts'][name]=state['counts'].get(name,0)+value
                for row in payload.get('rows',[]):
                    if row['disposition']=='shortlist':
                        catalog_rows.append({k:row.get(k) for k in ('id','field','area_um2','stage_xy_um','source_position_um','source_transform_sha256',
                            'source_tiff_sha256','source_shape_hw','preview','bbox_px','polygons_px','contrast_rgb','rank','disposition','sieve_results','focus_evidence',
                            'revisit_source_pixel_xy','revisit_point_inside_observed_region')})
                catalog_rows=sorted(catalog_rows,key=lambda r:r['rank'],reverse=True)[:1000];status()
            worker=read(run_folder/'scan-status.json') if (run_folder/'scan-status.json').exists() else {}
            acquisition_done=batch.get('state') in TERMINAL or worker.get('state') in TERMINAL
            state['acquisition_state']=batch.get('state') or worker.get('state')
            if args.once or acquisition_done:
                state.update(state='caught_up' if not acquisition_done else 'complete_available_committed_tiles',current_field=None,
                    completion_scope='Only available manifest-committed fields; acquisition may have ended early. No all-chips or no-flakes claim.');break
            state.update(state='waiting_for_committed_tiles',current_field=None);status()
            # Check independent STOP each second; never alter scanner STOP/owner.
            for _ in range(max(1,int(args.poll_seconds))):time.sleep(1);check()
    except AnalysisStop as exc:state.update(state='stopped',error=str(exc))
    except ResourcesPaused as exc:state.update(state='resource_paused',error=str(exc))
    except BaseException as exc:state.update(state='failed_closed',error=str(exc));raise
    finally:
        try:status()
        finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()
    return state


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--project',type=Path,required=True);parser.add_argument('--run-id',required=True)
    parser.add_argument('--model',type=Path);parser.add_argument('--field-timeout',type=float,default=240.);parser.add_argument('--poll-seconds',type=int,default=10)
    parser.add_argument('--once',action='store_true');args=parser.parse_args()
    if not 10<=args.field_timeout<=600 or not 1<=args.poll_seconds<=60:parser.error('Field timeout10..600s and poll1..60s required.')
    run(args)
