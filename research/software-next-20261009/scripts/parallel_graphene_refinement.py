"""Explicit opt-in two-core refinement with unchanged per-field image math."""
from pathlib import Path
import gc,json,os,time,uuid,ctypes
from concurrent.futures import ProcessPoolExecutor
import numpy as np
import cv2
from scipy.ndimage import find_objects
import analyze_graphene_run as driver
from flakefinder import graphene_useful_regions as refinement
from flakefinder import cpu_topology,memory_capacity


def process_field(source,chip,tile,source_entry,out,plan,plan_hash,cpu_mask):
    driver.cpu_policy(cpu_mask=cpu_mask)
    run_id=plan['run_id'];chip_name=chip.name;state={};status={};compact={};source_fields={}
    def report(*args):pass
    def check():
        if (out/'STOP').exists():raise driver.watcher.AnalysisStop('Independent useful-region refinement STOP')
    check()
    field_start=time.monotonic();fieldpath=source/source_entry['field_json']
    try:
        if driver.digest(fieldpath,check)!=source_entry['hashes'][source_entry['field_json']]:raise ValueError('Source field record changed.')
        field=driver.read(fieldpath,check);regions=[r for r in field['regions'] if r['area_um2']>=25.]
    except (driver.watcher.AnalysisStop,MemoryError):raise
    except Exception as exc:
        state[tile]={'state':'source_unavailable' if isinstance(exc,FileNotFoundError) else 'invalid_source',
            'reason':str(exc),'exception_type':type(exc).__name__,'source_field_path':str(fieldpath),
            'expected_source_field_sha256':source_entry['hashes'][source_entry['field_json']],
            'scientific_absence_not_inferred':True}
        pass # Only the parent writes the ledger.
        status.update(completed_fields=len(state),fields_complete=len(state),unavailable_fields=sum(r['state']=='source_unavailable' for r in state.values()))
        report(True);return state[tile]
    if not regions:
        state[tile]={'state':'no_regions_above_area_threshold'};return state[tile]
    folder=out/'fields'/(tile+'-'+uuid.uuid4().hex[:8]);folder.mkdir(parents=True)
    try:
        maskpath=fieldpath.with_name('masks.npz');relative=maskpath.relative_to(source).as_posix()
        if driver.digest(maskpath,check)!=source_entry['hashes'][relative]:raise ValueError('Source masks changed.')
        def load_masks():
            with np.load(maskpath,allow_pickle=False) as a:return a['labels'],a['valid_mask'],a['substrate_mask']
        labels,valid,support=driver.retry_read(load_masks,check)
        rawpath=chip/'tiles'/f'{tile}.tiff'
        if driver.digest(rawpath,check)!=field['source_hashes'][f'tiles/{tile}.tiff']:raise ValueError('Committed raw hash changed.')
        raw=driver.retry_read(lambda:cv2.imread(str(rawpath),cv2.IMREAD_UNCHANGED),check)
        if raw is None:raise ValueError('Raw decode failed.')
        factor=field['reduction']['factor'];h,w=labels.shape
        image=raw[:h*factor,:w*factor,::-1].reshape(h,factor,w,factor,3).mean(axis=(1,3),dtype=np.float32)
        del raw;pixel=np.asarray(field['reduction']['boundary_resolution_um']);prepared=refinement.prepare_field(image,valid,support,pixel)
        halo_records={}
        if field.get('classifier',{}).get('context',{}).get('material')=='hBN':
            from flakefinder.hbn_halo_screen import screen_halos
            halo_records=screen_halos(field['regions'],labels,prepared,pixel,valid_mask=valid,check_cancel=check)
        boxes=find_objects(labels);pad=int(np.ceil(12/min(pixel)));rows={};masks={};old_size=0
        for n,region in enumerate(regions):
            check();label=region['instance_label'];box=boxes[label-1]
            if box is None:raise ValueError('Region label missing from committed mask.')
            y0=max(0,box[0].start-pad);y1=min(h,box[0].stop+pad);x0=max(0,box[1].start-pad);x1=min(w,box[1].stop+pad)
            section=np.s_[y0:y1,x0:x1];original=labels[section]==label
            local={key:(value[section] if key!='noise' else value) for key,value in prepared.items()}
            result=refinement.refine_candidate(image[section],original,valid[section],pixel,substrate_mask=support[section],prepared=local)
            assessment=refinement.review_assessment(region,result,pixel);old_size+=assessment['metrics']['old_size_flag_removed']
            if field.get('classifier',{}).get('context',{}).get('material')=='hBN':
                from flakefinder.hbn_review_screen import morphology_screen
                assessment['hbn_screen']=(region['hbn_screen'] if region.get('hbn_screen',{}).get('policy_version')=='hbn-thin-film-evidence-v2' else morphology_screen(region,result,pixel,assessment['metrics']))
                halo=halo_records.get(label,{})
                assessment['hbn_screen']={**assessment['hbn_screen'],'halo_screen':halo}
                if halo.get('likely_halo'):
                    assessment['hbn_screen'].update(passes=False,thin_supported=False,
                        reasons=list(dict.fromkeys(assessment['hbn_screen'].get('reasons',[])+['edge_halo_around_stronger_feature'])))
                    assessment['likely_artifact']=True
                    assessment['artifact_flags']=list(dict.fromkeys(assessment['artifact_flags']+['edge_halo_around_stronger_feature']))
            candidate_id=f'{chip_name}/{tile}/{label}';geometry=[]
            for component in result['geometry']:
                for points in [component['polygon_px'],*component['holes_px']]:
                    geometry.append(((np.asarray(points,float)+[x0,y0])*factor+(factor-1)/2).tolist())
            accepted=result['status']=='edge_supported_proposal';original_geometry=[region['polygon_source_px'],*region['holes_source_px']]
            rows[candidate_id]={**assessment,'id':candidate_id,'polygons_px':geometry if accepted else original_geometry,
                'refined_polygons_px':geometry,'original_polygons_px':original_geometry,
                'refinement_status':'accepted' if accepted else result['status'],
                'source_field_sha256':source_entry['hashes'][source_entry['field_json']],
                'original_selected_for_review':region['selected_for_review'],'source_hashes':field['source_hashes'],
                'mask_key':f'r{label}','mask_shape_hw':list(original.shape),'analysis_crop_xywh':[x0,y0,x1-x0,y1-y0],
                'source_pixel_affine':[[factor,0,x0*factor+(factor-1)/2],[0,factor,y0*factor+(factor-1)/2]]}
            masks[f'r{label}']=np.packbits(result['refined_mask'].ravel())
            masks[f'u{label}']=np.packbits(result['uncertain_mask'].ravel())
            status.update(current_region=n+1,current_field_regions=len(regions));report()
        check();np.savez_compressed(folder/'masks.npz',**masks)
        shard={'schema':2,'run_id':run_id,'plan_sha256':plan['source_plan_sha256'],'refinement_plan_sha256':plan_hash,
            'field':chip_name+'/'+tile,
            'source_field_sha256':source_entry['hashes'][source_entry['field_json']],
            'source_mask_sha256':source_entry['hashes'][relative],'masks_sha256':driver.digest(folder/'masks.npz',check),'regions':rows}
        driver.watcher.save(folder/'regions.json',shard);shard_path=(folder/'regions.json').relative_to(out).as_posix();shard_hash=driver.digest(folder/'regions.json',check)
        field_index={key:{k:row[k] for k in ('rank','likely_artifact','refinement_status','artifact_flags','useful_size','source_field_sha256')} for key,row in rows.items()}
        for key,row in field_index.items():
            row.update(shard_path=shard_path,shard_sha256=shard_hash)
            if 'hbn_screen' in rows[key]:row['hbn_screen']=rows[key]['hbn_screen']
        driver.watcher.save(folder/'index.json',field_index)
        state[tile]={'state':'complete','region_count':len(rows),'old_size_flags_corrected':int(old_size),
            'source_field_sha256':shard['source_field_sha256'],'index_path':(folder/'index.json').relative_to(out).as_posix(),
            'index_sha256':driver.digest(folder/'index.json',check),'seconds':time.monotonic()-field_start}
        compact.update(field_index);source_fields[chip_name+'/'+tile]=shard['source_field_sha256']
        del rows,masks,prepared,image,labels,valid,support;gc.collect()
    except (driver.watcher.AnalysisStop,MemoryError):raise
    except Exception as exc:
        state[tile]={'state':'failed','reason':str(exc),'exception_type':type(exc).__name__,'regions_not_assessed':len(regions)}
    return state[tile]


def assignment():
    if os.name!='nt':return {'processor_masks':[None,None],'topology_source':'portable unpinned fallback'}
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.GetProcessAffinityMask.argtypes=[wintypes.HANDLE,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)]
    allowed=ctypes.c_size_t();system=ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(),ctypes.byref(allowed),ctypes.byref(system)):raise ctypes.WinError(ctypes.get_last_error())
    result=cpu_topology.processor_assignment(allowed.value,2)
    if len(result['processor_masks'])!=2:raise ValueError('Two allowed processors required for explicit parallel refinement.')
    return result


def run(args,execution_policy):
    source=args.analysis.resolve();project=source.parents[4];run_id=source.parents[1].name;chip_name=source.parent.name
    if not args.version.replace('-','').replace('_','').isalnum():raise ValueError('Simple distinct version required.')
    source_plan_sha=driver.digest(source/'plan.json')
    if execution_policy.get('schema')!=1 or execution_policy.get('workers')!=2 or execution_policy.get('version')!=args.version or execution_policy.get('source_plan_sha256')!=source_plan_sha:
        raise ValueError('Explicit refinement execution policy does not match this source/version.')
    out=source.parent/args.version
    if out==source:raise ValueError('Refinement must have separate output version.')
    out.mkdir(exist_ok=True);cpu=assignment();driver.cpu_policy(cpu_mask=cpu.get('affinity_mask'))
    lock=(out/'owner.lock').open('a+b');lock.write(b'0');lock.flush();lock.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    def check():
        if (out/'STOP').exists():raise driver.watcher.AnalysisStop('Independent useful-region refinement STOP')
    source_plan=driver.read(source/'plan.json',check);ledger=driver.read(source/'ledger.json',check)
    chip=project/'data'/run_id/chip_name;selected=sorted(ledger) if args.tiles=='all' else args.tiles.split(',')
    priority=args.priority_tiles.split(',') if args.priority_tiles else []
    if any(t not in selected for t in priority):raise ValueError('Priority fields must belong to selected manifest fields.')
    selected=list(dict.fromkeys(priority+selected))
    if any(t not in ledger for t in selected):raise ValueError('Choose exact saved field IDs.')
    sources=[Path(refinement.__file__).with_name('hbn_halo_screen.py'),Path(refinement.__file__).with_name('hbn_review_screen.py'),Path(__file__),Path(__file__).with_name('refine_graphene_regions.py'),Path(refinement.__file__),Path(driver.__file__),Path(cpu_topology.__file__)]
    plan={'schema':2,'kind':'useful_region_refinement_v2','run_id':run_id,'source_analysis':str(source),
        'source_plan_sha256':source_plan_sha,'plan_sha256':source_plan_sha,'source_ledger_sha256':driver.digest(source/'ledger.json',check),
        'source_manifest_sha256':source_plan['pin']['manifest_sha256'],'minimum_area_um2':25.,'tiles':selected,
        'options':refinement.asdict(refinement.RefinementOptions()),'threads':1,'workers':2,'execution_policy':execution_policy,
        'execution_policy_sha256':driver.watcher.canonical(execution_policy),
        'code_sha256':{str(p):driver.digest(p,check) for p in sources},
        'policy':'All saved regions >=25um2 independently of optical class. Exact per-field image math; two bounded processes, one index/ledger writer.'}
    if (out/'plan.json').exists():
        if driver.read(out/'plan.json',check)!=plan:raise ValueError('Pinned refinement source/config changed; choose new version.')
    else:driver.watcher.save(out/'plan.json',plan)
    plan_hash=driver.digest(out/'plan.json',check);state=driver.read(out/'ledger.json',check) if (out/'ledger.json').exists() else {}
    compact={};source_fields={};started=time.monotonic();new_fields=0;field_times=[]
    status={'state':'running','pid':os.getpid(),'version':args.version,'selected_fields':len(selected),'completed_fields':len(state),
        'refined_regions':0,'no_acquisition_performed':True,'fields_complete':len(state),'total_fields':len(selected),
        'regions_complete':0,'total_regions':None,'cpu_assignment':cpu}
    def report():
        status.update(elapsed_seconds=time.monotonic()-started,updated_at=time.time(),memory=driver.memory_snapshot())
        driver.watcher.save(out/'status.json',status)
    def publish(final=False):
        name=f'index-{len(state):06d}'+('-complete' if final else '')+'.json';path=out/name
        payload={'schema':2,'kind':'useful_region_refinement_index_v2','run_id':run_id,'plan_sha256':source_plan_sha,
            'refinement_plan_sha256':plan_hash,'source_fields':source_fields,'regions':compact,'progress':dict(status)}
        if not path.exists():driver.watcher.save(path,payload)
        driver.watcher.save(out/'latest.json',{'schema':1,'index_path':name,'sha256':driver.digest(path,check),
            'refinement_plan_sha256':plan_hash,'state':status['state']})
    def verify():
        check()
        for path,sha in [(chip/'manifest.json',plan['source_manifest_sha256']),(source/'plan.json',source_plan_sha),(source/'ledger.json',plan['source_ledger_sha256'])]:
            if driver.digest(path,check)!=sha:raise ValueError('Pinned refinement source changed: '+str(path))
        for path,sha in plan['code_sha256'].items():
            if driver.digest(path,check)!=sha:raise ValueError('Refinement code changed during execution: '+path)
    def incorporate(tile,entry):
        nonlocal new_fields
        if entry.get('index_path'):
            path=out/entry['index_path']
            if driver.digest(path,check)!=entry['index_sha256']:raise ValueError('Committed field index changed.')
            compact.update(driver.read(path,check));source_fields[chip_name+'/'+tile]=entry['source_field_sha256']
        state[tile]=entry;new_fields+=1
        status.update(completed_fields=len(state),fields_complete=len(state),regions_complete=len(compact),refined_regions=len(compact),
            failed_fields=sum(v['state']=='failed' for v in state.values()),unavailable_fields=sum(v['state']=='source_unavailable' for v in state.values()))
        driver.watcher.save(out/'ledger.json',state);report()
        if new_fields%10==0:publish()
    try:
        verify();report();pending=[]
        for tile in selected:
            if tile in state:
                entry=state[tile]
                if entry.get('index_path'):
                    path=out/entry['index_path']
                    if driver.digest(path,check)!=entry['index_sha256']:raise ValueError('Committed field index changed.')
                    compact.update(driver.read(path,check));source_fields[chip_name+'/'+tile]=entry['source_field_sha256']
            elif ledger[tile]['state']!='assessed':incorporate(tile,{'state':'source_unassessable','reason':ledger[tile].get('reason')})
            else:pending.append(tile)
        with ProcessPoolExecutor(max_workers=2) as pool:
            for start in range(0,len(pending),2):
                verify();batch=pending[start:start+2]
                capacity=memory_capacity.assess(memory_capacity.snapshot(),2*265*1024**2,purpose='two measured offline boundary workers')
                status['memory_capacity']=capacity
                if not capacity['adequate']:status.update(state='resource_paused',reason='Measured boundary working allocations exceed commit/address capacity.');report();return status
                status.update(current_field=','.join(batch));report()
                futures=[(tile,pool.submit(process_field,source,chip,tile,ledger[tile],out,plan,plan_hash,cpu['processor_masks'][slot])) for slot,tile in enumerate(batch)]
                for tile,future in futures:
                    try:entry=future.result()
                    except MemoryError:status.update(state='resource_paused',reason='Boundary allocation failed; incomplete output uncommitted.');report();return status
                    verify();incorporate(tile,entry)
        status.update(state='complete',current_field=None,completed_fields=len(state),fields_complete=len(state),regions_complete=len(compact),refined_regions=len(compact),
            total_regions=None if any(r['state']=='source_unavailable' for r in state.values()) else len(compact),
            completion_scope='All selected fields attempted; missing inputs remain unassessable. Original masks unchanged.')
        publish(True);report()
    except driver.watcher.AnalysisStop as exc:status.update(state='stopped',reason=str(exc));report()
    except BaseException as exc:status.update(state='failed_closed',reason=str(exc));report();raise
    finally:lock.close()
    return status
