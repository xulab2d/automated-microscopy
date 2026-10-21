"""Versioned offline graphene/hBN assessment of exact manifest-committed fields.

Never connects hardware or reconstructs missing acquisition sidecars. Derived
labels describe conditional optical populations, not verified graphene/layers.
"""
from pathlib import Path
import argparse,errno,gc,hashlib,json,os,re,sys,time,uuid

WORKSPACE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORKSPACE));sys.path.insert(0,str(Path(__file__).parent))
import watch_graphene_scan as watcher
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
VERSION='offline-graphene-blockmean-v1'
ASSESSMENT_STATES=('assessed','unassessable','budget_exhausted','invalid_source','failed')


class SourceEvidenceError(ValueError):
    """A required source identity/hash does not match the committed evidence."""


def assessment_failure(exc):
    from flakefinder.terrace_domain import DomainExclusion
    from flakefinder.model import AcquisitionError
    quality=watcher.QUALITY_MESSAGES|{
        'Nonpositive supported-domain background; fit rejected.',
        'Insufficient independent substrate samples after conservative block reduction.'}
    if isinstance(exc,watcher.FieldBudget):return 'budget_exhausted'
    if isinstance(exc,(SourceEvidenceError,json.JSONDecodeError)):return 'invalid_source'
    if isinstance(exc,FileNotFoundError):return 'unassessable'
    if isinstance(exc,DomainExclusion) or isinstance(exc,(AcquisitionError,ValueError)) and str(exc) in quality:
        return 'unassessable'
    if isinstance(exc,AcquisitionError) and str(exc)=='Missing or changed independently recorded substrate labels.':
        return 'invalid_source'
    return 'failed'


def retry_read(action,check=lambda:None):
    for attempt in range(5):
        check()
        try:return action()
        except OSError as exc:
            if exc.errno not in (errno.EINVAL,errno.ENOENT) or attempt==4:raise
            for _ in range(2**attempt):check();time.sleep(.1)


def digest(path,check=lambda:None):
    def once():
        h=hashlib.sha256()
        with Path(path).open('rb') as stream:
            while True:
                check();block=stream.read(1024**2)
                if not block:break
                h.update(block)
        return h.hexdigest()
    return retry_read(once,check)


def read(path,check=lambda:None):
    return retry_read(lambda:json.loads(Path(path).read_text(encoding='utf-8-sig')),check)


def select_fields(manifest,selection):
    committed=manifest.get('completed',{})
    if not committed:raise ValueError('No manifest-committed fields.')
    tiles=sorted(committed) if selection=='all' else selection.split(',')
    if len(set(tiles))!=len(tiles) or any(not re.fullmatch(r'tile_\d{6}',t) or t not in committed for t in tiles):
        raise ValueError('Select unique exact manifest tile IDs or all.')
    return tiles


def reduce_arrays(arrays,factor=2):
    """Block means with conservative support; trailing incomplete blocks excluded."""
    import numpy as np
    from sklearn.covariance import OAS
    if type(factor) is not int or factor<1:raise ValueError('Positive integer reduction required.')
    h,w=arrays['valid_mask'].shape;rh,rw=h//factor,w//factor
    if min(rh,rw)<1:raise ValueError('Reduction exceeds source dimensions.')
    def blocks(value):
        return value[:rh*factor,:rw*factor].reshape(rh,factor,rw,factor,*value.shape[2:])
    reduced={'contrast_rgb':blocks(arrays['contrast_rgb']).mean(axis=(1,3),dtype=np.float32),
        'valid_mask':blocks(arrays['valid_mask']).all(axis=(1,3)),
        'substrate_mask':blocks(arrays['substrate_mask']).all(axis=(1,3)),
        'saturated_mask':blocks(arrays['saturated_mask']).any(axis=(1,3)),
        'pixel_size_um':np.asarray(arrays['pixel_size_um'])*factor}
    support=reduced['substrate_mask']&reduced['valid_mask']&~reduced['saturated_mask']
    reduced['substrate_mask']=support
    samples=reduced['contrast_rgb'][support]
    if len(samples)<100:raise ValueError('Insufficient independent substrate samples after conservative block reduction.')
    samples=samples[::max(1,int(np.ceil(len(samples)/20000)))]
    reduced['noise_covariance']=OAS().fit(samples).covariance_+np.eye(3)*1e-12
    info={'factor':factor,'source_shape_hw':[h,w],'analysis_shape_hw':[rh,rw],
        'source_pixel_from_analysis_pixel':[[factor,0,(factor-1)/2],[0,factor,(factor-1)/2]],
        'omitted_bottom_rows':h-rh*factor,'omitted_right_columns':w-rw*factor,
        'mask_policy':'valid and substrate: all source pixels; saturated: any source pixel',
        'noise_method':'OAS of independently supported reduced contrast samples; no division by region area',
        'noise_samples':len(samples),'boundary_resolution_um':reduced['pixel_size_um'].tolist(),
        'boundary_qualification':'Contours join reduced block centres; mask cells span factor by factor source pixels. Sub-block boundaries are unresolved.'}
    return reduced,info


def source_geometry(region,labels,entry,reduction,section=None):
    import numpy as np
    factor=reduction['factor'];offset=(factor-1)/2
    def scaled(points):return (np.asarray(points,float).reshape(-1,2)*factor+offset).tolist()
    if section is None:yy,xx=np.nonzero(labels==region['instance_label'])
    else:
        yy,xx=np.nonzero(labels[section]==region['instance_label']);yy+=section[0].start;xx+=section[1].start
    if not len(xx):raise ValueError('Region label has no observed pixels.')
    middle=len(xx)//2;point=np.array([xx[middle],yy[middle]],float)*factor+offset
    h,w=reduction['source_shape_hw'];position=entry['record']['position']
    stage=np.asarray(entry['transform']['matrix'])@(point-[(w-1)/2,(h-1)/2])+[position['x'],position['y']]
    return {'polygon_source_px':scaled(region['polygon_px']),
        'holes_source_px':[scaled(hole) for hole in region['holes_px']],
        'revisit_source_pixel_xy':point.tolist(),'stage_xy_um':stage.tolist(),
        'source_position_um':position,'source_shape_hw':[h,w],
        'revisit_point_inside_observed_reduced_mask':True,
        'coordinate_qualification':'Historical source coordinates only; acquisition is paused and placement must be re-established before any revisit.'}


def field_entry(chip,tile,manifest):
    record=manifest['completed'][tile]
    required=[f'tiles/{tile}.tiff',f'tiles/{tile}.json',f'boundary-labels/{tile}.npz']
    return {'field':chip.name+'/'+tile,'chip':chip,'tile':tile,'record':record,
        'record_sha256':watcher.canonical(record),'source_hashes':{p:record.get('hashes',{}).get(p) for p in required},
        'transform':manifest['transform'],'context':manifest.get('context',{})}


def _windows_memory_snapshot():
    import ctypes
    from ctypes import wintypes
    from flakefinder.acquisition_resources import available_memory_bytes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
            *[(name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
                'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage',
                'PagefileUsage','PeakPagefileUsage','PrivateUsage')]]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);psapi=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.GetCurrentProcess.argtypes=[];kernel.GetCurrentProcess.restype=wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
    counters=Counters();counters.cb=ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'rss_bytes':int(counters.WorkingSetSize),'process_peak_rss_bytes':int(counters.PeakWorkingSetSize),
        'private_bytes':int(counters.PrivateUsage),'available_host_bytes':available_memory_bytes(),
        'measurement':'Windows GetProcessMemoryInfo; process peak is cumulative'}


def memory_snapshot():
    """Windows peak working set is cumulative for this process, not phase-local."""
    try:
        import psutil
        info=psutil.Process().memory_info()
        return {'rss_bytes':info.rss,'process_peak_rss_bytes':getattr(info,'peak_wset',None),
            'private_bytes':getattr(info,'private',None),'available_host_bytes':psutil.virtual_memory().available}
    except ImportError:
        if os.name=='nt':
            try:return _windows_memory_snapshot()
            except OSError as exc:return {'measurement':'native memory query unavailable','error':str(exc)}
        return {'measurement':'psutil unavailable; native fallback supports Windows'}


def verify_field(entry,check):
    for name,expected in entry['source_hashes'].items():
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
            raise SourceEvidenceError('Missing committed source hash: '+name)
        source=entry['chip']/name
        if source.resolve()!=source:raise SourceEvidenceError('Redirected source sidecar.')
        if digest(source,check)!=expected:raise SourceEvidenceError('Committed source hash mismatch: '+name)


def segment_reduced_input(reduced,model,classifier,material,check):
    """Select explicit image geometry without changing default optical masks."""
    from flakefinder.material_terraces import MaterialTerraceOptions,segment_material_terraces
    method=model.get('geometry_method');image=None
    if method not in (None,'observed_rgb_v1'):raise ValueError('Unsupported explicit geometry method')
    if method=='observed_rgb_v1':
        from flakefinder.hbn_image_regions import image_regions
        image=image_regions(reduced['contrast_rgb'],reduced['valid_mask']&~reduced['saturated_mask'],
            reduced['substrate_mask'],reduced['noise_covariance'],reduced['pixel_size_um'],check_cancel=check)
    result=segment_material_terraces(reduced['contrast_rgb'],reduced['valid_mask'],reduced['substrate_mask'],
        reduced['noise_covariance'],reduced['pixel_size_um'],classifier,saturated_mask=reduced['saturated_mask'],
        parent_labels=image['labels'] if image is not None else None,
        options=MaterialTerraceOptions(useful_area_um2=25.,merge_material_populations=material=='hBN' and image is None,
            preserve_parent_regions=image is not None),check_cancel=check)
    if image is not None:
        result['image_parent_labels']=image['parent_labels']
        source={row['instance_label']:row for row in image['regions']}
        for row in result['regions']:
            observed=source[row['parent_instance_label']]
            row.update(image_parent_instance_label=observed['parent_instance_label'],
                image_photometric_mode=observed['photometric_mode'],geometry_basis=observed['geometry_basis'])
        result['report']['image_geometry']={**image['report'],'diagnostics':image['diagnostics']}
    return result


def analyze_field(entry,model,attempt,args,check):
    check()
    import numpy as np
    from flakefinder.material_input import prepare_material_input
    from flakefinder.material_contrast import MaterialContrastClassifier
    verify_field(entry,check)
    material=entry['context'].get('material')
    context=retry_read(lambda:watcher.input_context(entry,args.run_id,material=material),check)
    if model.get('empirical_trajectory'):
        if material!='graphene':raise ValueError('Graphene empirical bands cannot classify another material.')
        from flakefinder.graphene_empirical import EmpiricalGrapheneClassifier
        classifier_type=EmpiricalGrapheneClassifier
    else:classifier_type=MaterialContrastClassifier
    classifier=classifier_type(model,context=context,model_covariance=np.eye(3)*.01**2,
        allow_context_mismatch=args.exploratory_context_mismatch)
    memory={'before_preparation':memory_snapshot()}
    arrays,provenance=retry_read(lambda:prepare_material_input(chip_folder=entry['chip'],tile_id=entry['tile'],
        context=context,maximum_pixels=6_000_000),check)
    memory['after_full_resolution_preparation']=memory_snapshot()
    check();reduced,reduction=reduce_arrays(arrays,args.factor);del arrays;gc.collect();check()
    memory['after_reduction']=memory_snapshot()
    result=segment_reduced_input(reduced,model,classifier,material,check)
    memory['after_segmentation']=memory_snapshot()
    check();verify_field(entry,check)
    from scipy.ndimage import find_objects
    sections=find_objects(result['labels'])
    rows=[{**region,**source_geometry(region,result['labels'],entry,reduction,sections[region['instance_label']-1])} for region in result['regions']]
    evidence_binding=None
    if material=='hBN' and model.get('geometry_method')=='observed_rgb_v1':
        from flakefinder.hbn_region_evidence import compile_region_evidence,attach_region_evidence
        preview=entry['chip']/'tiles'/(entry['tile']+'.jpg')
        preview_hash=digest(preview,check) if preview.is_file() else None
        rows,evidence_binding=attach_region_evidence(rows,result['labels'],field=entry['field'],
            source_preview_sha256=preview_hash,reduction=reduction,compiled=compile_region_evidence(model),
            source_hashes=entry['source_hashes'])
    attempt.mkdir(parents=True,exist_ok=False)
    with (attempt/'masks.npz').open('xb') as stream:
        np.savez_compressed(stream,**{k:result[k] for k in ('labels','parent_labels','class_map','material_compatible_mask','debris_compatible_mask')},
            **({'image_parent_labels':result['image_parent_labels']} if 'image_parent_labels' in result else {}),
            **{k:reduced[k] for k in ('valid_mask','substrate_mask','saturated_mask')})
    payload={'assessment_state':'assessed','field':entry['field'],'regions':rows,'report':result['report'],
        'source_hashes':entry['source_hashes'],'source_record_sha256':entry['record_sha256'],
        'source_transform':entry['transform'],'prepared_input':provenance,'reduction':reduction,
        'classifier':classifier.metadata,'reduced_noise_covariance':reduced['noise_covariance'].tolist(),
        'memory':memory,'model_qualification':model.get('qualification','Unvalidated conditional optical compatibility; contamination may match. No empirical debris model is implied.'),
        'background_systematic_covariance':'Unquantified, not zero; conditional optical compatibility only.',
        'material_identity_confirmed':False,'layer_assignment_allowed':False,
        **({'hbn_evidence_binding':evidence_binding} if evidence_binding is not None else {})}
    watcher.save(attempt/'field.json',payload);check()
    return payload


def cpu_policy(cpu_mask=None,allowed_cpu_count=1):
    if type(allowed_cpu_count) is not int or allowed_cpu_count<1:
        raise ValueError('Positive allowed CPU count required.')
    if cpu_mask is not None and (type(cpu_mask) is not int or cpu_mask<=0):
        raise ValueError('Positive explicit CPU mask required.')
    import cv2
    from threadpoolctl import threadpool_limits
    cv2.setNumThreads(1);threadpool_limits(1);priority=watcher.background_priority()
    assignment=None;cpu_masks=[None]*allowed_cpu_count
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        from flakefinder.cpu_topology import processor_assignment
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);handle=kernel.GetCurrentProcess
        handle.restype=wintypes.HANDLE;process=handle();mask=ctypes.c_size_t();system=ctypes.c_size_t()
        kernel.GetProcessAffinityMask.argtypes=[wintypes.HANDLE,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
        kernel.SetProcessAffinityMask.argtypes=[wintypes.HANDLE,ctypes.c_size_t]
        if not kernel.GetProcessAffinityMask(process,ctypes.byref(mask),ctypes.byref(system)):raise ctypes.WinError(ctypes.get_last_error())
        if cpu_mask is None:
            assignment=processor_assignment(mask.value,allowed_cpu_count)
            selected=assignment['affinity_mask'];available=assignment['processor_masks']
            cpu_masks=[available[i%len(available)] for i in range(allowed_cpu_count)]
            assignment={**assignment,'worker_masks_reused':len(available)<allowed_cpu_count}
        else:
            # A reused worker may currently be pinned to the other dispatched core.
            # Keep the parent's explicit assignment, within this processor group.
            if cpu_mask & ~system.value:raise ValueError('Explicit CPU mask outside system affinity.')
            selected=cpu_mask;cpu_masks=[cpu_mask]
        if not kernel.SetProcessAffinityMask(process,selected):
            raise ctypes.WinError(ctypes.get_last_error())
    return {'threads':1,'priority':priority,
        'windows_affinity':'explicit bounded CPU mask' if cpu_mask is not None else 'physical cores preferred within allowed mask',
        'cpu_masks':cpu_masks,'cpu_topology':assignment}


def run(args):
    from flakefinder.storage import resolve_output_path
    from flakefinder.acquisition_resources import available_memory_bytes
    if not re.fullmatch(r'chip\d+',args.chip) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.version):
        raise ValueError('Simple exact chip and explicit analysis version required.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}',args.run_id):raise ValueError('Exact simple run ID required.')
    request=read(Path(args.project)/'data'/args.run_id/'scan-request.json')
    material=request.get('settings',{}).get('material')
    run_folder,identity=retry_read(lambda:watcher.request_identity(args.project,args.run_id,material=material))
    chip=run_folder/args.chip
    if chip.resolve()!=chip:raise ValueError('Redirected chip source.')
    manifest=read(chip/'manifest.json');tiles=select_fields(manifest,args.tiles)
    if manifest.get('context',{}).get('material')!=material or manifest['context'].get('oxide_nm')!=90:
        raise ValueError('Saved manifest material/oxide differs from the scan request.')
    if material=='hBN' and not args.model:raise ValueError('hBN requires an explicit hBN optical model.')
    namespace='hbn' if material=='hBN' else 'graphene'
    output=resolve_output_path(Path(args.project)/'analysis'/namespace/args.run_id/args.chip/args.version)
    output.mkdir(parents=True,exist_ok=True)
    lock=(output/'owner.lock').open('a+b');lock.write(b'0');lock.flush();lock.seek(0)
    if os.name=='nt':
        import msvcrt
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:lock.close();raise RuntimeError('This analysis version already has an owner.')
    else:
        import fcntl
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    state={'state':'starting','pid':os.getpid(),'output':str(output),'updated_at':time.time(),'no_acquisition_performed':True}
    def status():state['updated_at']=time.time();watcher.save(output/'status.json',state)
    def check():
        if (output/'STOP').exists():raise watcher.AnalysisStop('Independent offline-analysis STOP requested.')
    try:
        check();state['cpu']=cpu_policy(allowed_cpu_count=2) if getattr(args,'workers',1)==2 else cpu_policy();status()
        names=('cpu_topology.py','graphene_empirical.py','material_input.py','material_terraces.py','material_contrast.py','material_profiles.py','mote2_optics.py',
            'terrace_domain.py','terrace_analysis.py','imaging.py','model.py','hbn_image_regions.py','hbn_region_evidence.py','hbn_thin_evidence.py')
        sources=[Path(__file__),Path(__file__).with_name('offline_graphene_parallel.py'),Path(watcher.__file__),*[WORKSPACE/'flakefinder'/n for n in names],
            WORKSPACE/f'configs/materials/{namespace}-sio2-90nm.json',*sorted((WORKSPACE/'configs/materials/data').glob('*'))]
        code={str(p):digest(p,check) for p in sources if p.is_file()}
        geometry_method=read(args.model,check).get('geometry_method') if args.model else None
        config={'factor':args.factor,'tiles':tiles,'field_timeout_s':args.field_timeout,'geometry_method':geometry_method,
            'exploratory_context_mismatch':args.exploratory_context_mismatch,'min_free_mib':args.min_free_mib,
            'workers':getattr(args,'workers',1),
            'segmentation':'MaterialTerraceOptions defaults, useful_area_um2=25',
            'merge_material_populations':material=='hBN' and geometry_method!='observed_rgb_v1','model_covariance_rgb_diagonal':.01**2}
        pin={'version':VERSION,'identity':identity,'chip':args.chip,'manifest_sha256':digest(chip/'manifest.json',check),
            'code_sha256':code,'config':config,'config_sha256':watcher.canonical(config),
            'external_model_sha256':digest(args.model,check) if args.model else None}
        if (output/'plan.json').exists():
            plan=read(output/'plan.json',check)
            if plan['pin']!=pin:raise ValueError('Pinned input/config/code changed; choose a new analysis version.')
            model=read(output/'model.json',check)
            if watcher.canonical(model)!=plan['model_sha256']:raise ValueError('Saved optical model changed.')
        else:
            model=read(args.model,check) if args.model else watcher.prepare_model(output)
            context=model.get('applicability_context',model)
            if context.get('material')!=material or context.get('oxide_nm')!=90:
                raise ValueError('Optical model material/oxide differs from the saved scan request.')
            watcher.save(output/'model.json',model)
            watcher.save(output/'plan.json',{'pin':pin,'model_sha256':watcher.canonical(model),
                'source_immutable':True,'layer_assignment_allowed':False,'chemical_identity_confirmed':False})
        ledger=read(output/'ledger.json',check) if (output/'ledger.json').exists() else {}
        def verify_sources():
            if digest(chip/'manifest.json',check)!=pin['manifest_sha256']:raise ValueError('Pinned acquisition manifest changed.')
            if digest(run_folder/'scan-request.json',check)!=identity['scan_request_sha256']:raise ValueError('Pinned acquisition request changed.')
            for path,expected in code.items():
                if digest(path,check)!=expected:raise ValueError('Analysis source changed during execution.')
        if getattr(args,'workers',1)==2:
            from offline_graphene_parallel import run_fields
            run_fields(tiles,chip,manifest,model,args,output,ledger,state,check=check,status=status,verify_sources=verify_sources,cpu_masks=state['cpu']['cpu_masks'])
        else:
            for tile in tiles:
                check();state.update(state='running',current_field=tile,finished_fields=len(ledger),selected_fields=len(tiles));status()
                if tile in ledger:
                    for name,expected in ledger[tile]['hashes'].items():
                        if digest(output/name,check)!=expected:raise ValueError('Previously committed output changed: '+name)
                    continue
                if available_memory_bytes()<args.min_free_mib*1024**2:
                    state.update(state='resource_paused',reason='Available RAM below configured offline field-start reserve.');break
                if digest(chip/'manifest.json',check)!=pin['manifest_sha256']:raise ValueError('Pinned acquisition manifest changed.')
                if digest(run_folder/'scan-request.json',check)!=identity['scan_request_sha256']:raise ValueError('Pinned acquisition request changed.')
                for path,expected in code.items():
                    if digest(path,check)!=expected:raise ValueError('Analysis source changed during execution.')
                entry=field_entry(chip,tile,manifest);started=time.monotonic();attempt=output/'fields'/(tile+'-'+uuid.uuid4().hex[:8])
                def field_check():
                    check()
                    if time.monotonic()-started>args.field_timeout:raise watcher.FieldBudget('Offline field time budget exceeded.')
                try:
                    payload=analyze_field(entry,model,attempt,args,field_check)
                except watcher.AnalysisStop:raise
                except MemoryError:
                    state.update(state='resource_paused',reason='Field allocation failed; incomplete output is uncommitted.');break
                except Exception as exc:
                    attempt.mkdir(parents=True,exist_ok=True)
                    payload={'assessment_state':assessment_failure(exc),
                        'field':entry['field'],'reason':str(exc),'exception_type':type(exc).__name__,
                        'source_hashes':entry['source_hashes'],'source_features_not_disproved':True,'regions':None,
                        'memory_at_failure':memory_snapshot()}
                    watcher.save(attempt/'field.json',payload)
                check()
                if digest(chip/'manifest.json',check)!=pin['manifest_sha256']:raise ValueError('Pinned acquisition manifest changed during analysis.')
                if digest(run_folder/'scan-request.json',check)!=identity['scan_request_sha256']:raise ValueError('Pinned acquisition request changed during analysis.')
                ledger[tile]={'state':payload['assessment_state'],'reason':payload.get('reason'),
                    'field_json':(attempt/'field.json').relative_to(output).as_posix(),
                    'regions':len(payload['regions']) if payload.get('regions') is not None else None,
                    'hashes':{p.relative_to(output).as_posix():digest(p,check) for p in attempt.iterdir() if p.is_file()}}
                watcher.save(output/'ledger.json',ledger);del payload;gc.collect()
            else:state.update(state='complete',current_field=None)
        state.update(finished_fields=len(ledger),selected_fields=len(tiles),
            counts={key:sum(v['state']==key for v in ledger.values()) for key in ASSESSMENT_STATES})
        watcher.save(output/'summary.json',{**state,'fields':ledger,'scope':'Exact selected manifest fields; unassessable fields are not negative findings.'})
    except watcher.AnalysisStop as exc:state.update(state='stopped',reason=str(exc))
    except BaseException as exc:state.update(state='failed_closed',reason=str(exc));raise
    finally:
        try:status()
        finally:lock.close()
    return state


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,required=True);p.add_argument('--run-id',required=True)
    p.add_argument('--chip',required=True);p.add_argument('--version',required=True);p.add_argument('--tiles',default='all')
    p.add_argument('--factor',type=int,choices=(1,2,3,4),default=2);p.add_argument('--model',type=Path)
    p.add_argument('--exploratory-context-mismatch',action='store_true');p.add_argument('--field-timeout',type=float,default=600.)
    p.add_argument('--min-free-mib',type=int,default=768)
    p.add_argument('--workers',type=int,choices=(1,2),default=1)
    return p


if __name__=='__main__':
    args=parser().parse_args()
    if not 10<=args.field_timeout<=3600 or args.min_free_mib<256:raise SystemExit('Require timeout10..3600s and RAM reserve>=256MiB.')
    print(json.dumps(run(args),sort_keys=True))
