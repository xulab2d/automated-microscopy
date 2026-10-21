"""Prepared, resumable saved-mask campaign. Never imports a hardware adapter."""
from pathlib import Path
import argparse,collections,hashlib,importlib.util,json,os,shutil,sys,time,uuid

HERE=Path(__file__).resolve().parent
PROJECT=HERE.parents[1]
VERSION='saved-mask-sieve-campaign-v1'

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def save(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x',encoding='utf8') as f:json.dump(obj,f,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)

def replace_once(text,old,new):
    if text.count(old)!=1:raise ValueError('Frozen worker source no longer matches transformation anchor: '+old[:70])
    return text.replace(old,new)

def transform_worker(text):
    text=replace_once(text,'ROOT=Path(__file__).resolve().parent','ROOT=Path(__file__).resolve().parent\nBATCH_CHECK = lambda: None\nBATCH_INPUT = {}')
    text=replace_once(text,"atomic_json=lambda path,data:save_json(path,_plain(data))","atomic_json=lambda path,data:(BATCH_CHECK(),save_json(path,_plain(data)))[1]")
    text=replace_once(text,"shard_path=folder/'analysis-staged-features/fields'/(chip+'__'+tile+'.json')","shard_path=Path(BATCH_INPUT['shard'])")
    text=replace_once(text,"original=folder/'analysis-staged-features'/(chip+'__'+tile+'.instances.png')","original=Path(BATCH_INPUT['mask'])")
    text=replace_once(text,"repair=a.campaign.parent.parent/'research/topology-split-review-01/rays-v5'","repair=ROOT/'topology-repair'")
    text=replace_once(text,'for label,section in enumerate(find_objects(labels),1):','for label,section in enumerate(find_objects(labels),1):\n            BATCH_CHECK()')
    text=replace_once(text,"comparisons=comparator.compare(np.asarray([r['contrast_rgb'] for r in measurable]),np.asarray([r['metrics']['contrast_covariance'] for r in measurable]),eligibility=eligible)\n            for r,comparison in zip(measurable,comparisons['rows']):r['color_comparison']=comparison",
        "qualified=[r for r,ok in zip(measurable,eligible) if ok]\n            for r in measurable:r['color_comparison']={'state':'unassessed_ineligible_color'}\n            if qualified:\n                comparisons=comparator.compare(np.asarray([r['contrast_rgb'] for r in qualified]),np.asarray([r['metrics']['contrast_covariance'] for r in qualified]),eligibility=np.ones(len(qualified),bool))\n                for r,comparison in zip(qualified,comparisons['rows']):r['color_comparison']=comparison")
    text=replace_once(text,"maskfile=out/'masks'/(chip+'__'+tile+'.png');cv2.imwrite(str(maskfile),np.uint16(labels))",
        "maskfile=original\n        if key=='chip001/tile_000139':\n            BATCH_CHECK();maskfile=out/'masks'/(chip+'__'+tile+'.png')\n            if not cv2.imwrite(str(maskfile),np.uint16(labels)):raise IOError('Derived mask write failed')")
    text=replace_once(text,"'saved_mask_sha256':digest(maskfile)","'saved_mask_path':str(maskfile),'saved_mask_is_original':maskfile==original,'saved_mask_sha256':digest(maskfile)")
    text=replace_once(text,"atomic_json(out/'results.json',{'schema_version':1,'source_root':str(a.campaign),'rows':rows,'summary':summary,'field_reports':reports})",
        "atomic_json(out/'completion.json',{'schema_version':1,'source_root':str(a.campaign),'summary':summary,'field_reports':reports})")
    text=replace_once(text,"    (out/'source').mkdir(exist_ok=True)\n    for name in ('run_sieves.py','feature_sieves.py','transparent_film.py'):shutil.copy2(ROOT/name,out/'source'/name)",'')
    return text

def output_guard(out,reserve_gib,allowance=32*1024**2):
    if not Path(out).resolve().is_relative_to(PROJECT.resolve()):raise ValueError('All outputs must stay in managed Drive project')
    marker=json.loads((PROJECT/'.flakefinder-project.json').read_text())
    if marker.get('project_id')!='xu-lab-automated-microscopy-20261009':raise ValueError('Drive project identity mismatch')
    if shutil.disk_usage('C:/').free<int(reserve_gib*1024**3)+allowance:raise StoragePause('Drive cache cannot preserve offline reserve')
    if shutil.disk_usage(PROJECT).free<allowance:raise StoragePause('Drive mount has insufficient write headroom')

class StopRequested(Exception):pass
class StoragePause(Exception):pass

# These exact scientific refusals mean the field cannot be measured. Missing
# bytes, unsupported profiles, numerical/programming errors remain failures.
QUALITY_REFUSALS = {
    ('AcquisitionError', 'No sufficient independently recorded bare-substrate support; no color measurement or zero-contrast pass.'): 'insufficient_recorded_substrate',
    ('AcquisitionError', 'Insufficient independent substrate samples.'): 'insufficient_substrate_samples',
    ('AcquisitionError', 'Insufficient retained substrate samples.'): 'insufficient_retained_substrate',
    ('AcquisitionError', 'Substrate support cannot sustain spatial held-out fit validation.'): 'insufficient_spatial_support',
    ('ValueError', 'Insufficient matched-scale independent background samples'): 'insufficient_matched_scale_background',
}

def quality_reason(exc):
    module = type(exc).__module__
    if module not in ('flakefinder.model', 'builtins'):
        return None
    return QUALITY_REFUSALS.get((type(exc).__name__, str(exc)))

def record_unassessable(attempt, entry, exc, reason):
    """Commit an explicit lack of evidence, never an empty successful detection."""
    report = {'field': entry['field'], 'excluded': str(exc), 'reason': reason,
              'assessment_state': 'unassessable', 'source_shard': entry['shard'],
              'source_mask': entry['mask'], 'source_hashes': entry['hashes'],
              'source_features_retained': True, 'color_measurement_performed': False,
              'error_type': type(exc).__name__, 'recorded_at': time.time()}
    summary = {'counts': {}, 'sieve_failures': {}, 'assessed_regions': 0, 'elapsed_seconds': 0}
    save(attempt/'quality-exclusion.json', report)
    save(attempt/'completion.json', {'schema_version': 1, 'summary': summary, 'field_reports': [report]})
    # The separate quality record cannot be confused with an assessed shard.
    return {'folder': str(attempt), 'summary': summary, 'excluded': True,
            'unassessable': True, 'reason': reason,
            'hashes': {str(p): digest(p) for p in attempt.rglob('*') if p.is_file()}}

def inventory(workspace,campaign):
    entries={};unavailable=[];manifests={}
    roots=[(p.name,p/'analysis-staged-features') for p in sorted(campaign.glob('chip*')) if p.is_dir()]
    for version in ('recovery-v1','recovery-v2'):
        roots.append(('chip001',workspace/'research/staged-analysis-audit'/version))
    for chip,root in roots:
        for shard in sorted((root/'fields').glob('*.json')):
            try:
                raw=json.loads(shard.read_text())
                if 'rows'not in raw:continue  # Explicit recovery exclusions are not successful extracted fields.
                tile=raw['tile']['tile_id'];key=chip+'/'+tile
                if key in entries:raise ValueError('Duplicate successful source field: '+key)
                if 'rows'not in raw or 'mask_sha256'not in raw:raise ValueError('Not a completed extracted field')
                mask=root/(chip+'__'+tile+'.instances.png');source=campaign/chip/'tiles'/(tile+'.tiff')
                sidecar=source.with_suffix('.json');manifest=campaign/chip/'manifest.json'
                if str(manifest) not in manifests:manifests[str(manifest)]={'sha256':digest(manifest),'data':json.loads(manifest.read_text())}
                expected=raw['provenance']['source_files_sha256'];boundary=campaign/chip/'boundary-labels'/(tile+'.npz')
                boundary_hash=manifests[str(manifest)]['data']['completed'][tile]['hashes']['boundary-labels/'+boundary.name]
                paths={str(shard):digest(shard),str(mask):raw['mask_sha256'],str(source):expected['.tiff'],str(sidecar):expected['.json'],str(boundary):boundary_hash,str(manifest):manifests[str(manifest)]['sha256']}
                missing=[p for p in paths if not Path(p).is_file()]
                if missing:raise ValueError('Missing retained source: '+str(missing))
                entries[key]={'field':key,'shard':str(shard),'mask':str(mask),'hashes':paths,'source_family':root.name}
            except (OSError,ValueError,KeyError,TypeError) as exc:unavailable.append({'source':str(shard),'error':str(exc)})
    all_acquired={chip.name+'/'+tile for chip in campaign.glob('chip*') if (chip/'manifest.json').is_file() for tile in json.loads((chip/'manifest.json').read_text()).get('completed',{})}
    return list(entries.values()),unavailable,sorted(all_acquired-set(entries))

def prepare(args):
    out=args.output.resolve();output_guard(out,args.reserve_gib)
    if (out/'plan.json').exists():raise FileExistsError('Prepared plan exists; use run or a new output')
    snapshots=out/'source';snapshots.mkdir(parents=True,exist_ok=True)
    expected=json.loads((PROJECT/'data/whole-chip-standardized-01/analysis-residue-sieves-02/results.json').read_text())['summary']['source_code_sha256']
    source_hashes={}
    for name in ('run_sieves.py','feature_sieves.py','transparent_film.py'):
        src=HERE/name;sha=digest(src)
        if expected[name]!=sha:raise ValueError('Module differs from reviewed final02: '+name)
        source_hashes[str(src)]=sha;shutil.copyfile(src,snapshots/name)
    script=transform_worker((HERE/'run_sieves.py').read_text());(snapshots/'run_sieves_batch_worker.py').write_text(script,encoding='utf8')
    for name in ('curves.json','curves.npz'):
        src=HERE/'cache'/name;dest=snapshots/'cache'/name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(src,dest);source_hashes[str(src)]=digest(src)
    for src in sorted((args.workspace/'flakefinder').rglob('*.py')):
        dest=snapshots/'workspace'/src.relative_to(args.workspace);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest);source_hashes[str(src)]=digest(src)
    repair=PROJECT/'research/topology-split-review-01/rays-v5'
    for src in [repair/'report.json',*sorted(repair.glob('*-instances.png')),*sorted(repair.glob('*-unclassified.png'))]:
        dest=snapshots/'topology-repair'/src.name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(src,dest);source_hashes[str(src)]=digest(src)
    entries,missing,unextracted=inventory(args.workspace,args.campaign)
    plan={'version':VERSION,'prepared_at':time.time(),'workspace':str(args.workspace.resolve()),'campaign':str(args.campaign.resolve()),'output':str(out),
        'reserve_gib':args.reserve_gib,'fields':entries,'source_hashes':source_hashes,'source_failures':missing,'acquired_without_successful_mask':unextracted,
        'frozen_hashes':{str(p.relative_to(out)):digest(p) for p in snapshots.rglob('*') if p.is_file()},
        'supervisor_sha256':digest(__file__),'reuse_policy':'Process all fields; prior six results are not silently adopted.',
        'estimate_seconds_from_six_field_mean':len(entries)*62.26826979999896/6,'no_hardware':True}
    save(out/'plan.json',plan);save(out/'source-failures.json',missing)
    save(out/'status.json',{'state':'prepared_not_started','processed_fields':0,'total_fields':len(entries),'counts':{},'sieve_failures':{},'updated_at':time.time(),'current_field':None,'source_failure_count':len(missing),'analysis_failure_count':0,'successful_fields':0,'excluded_fields':0,'elapsed_seconds':0})
    print(json.dumps({'output':str(out),'total_fields':len(entries),'by_chip':dict(collections.Counter(e['field'].split('/')[0] for e in entries)),'acquired_without_successful_mask':len(unextracted),'source_inventory_failures':len(missing),'estimate_compute_hours':plan['estimate_seconds_from_six_field_mean']/3600},indent=2))

def verify_files(files):
    for name,expected in files.items():
        if not Path(name).is_file() or digest(name)!=expected:raise ValueError('Source hash mismatch: '+name)

def optical_profile(campaign,key):
    chip,tile=key.split('/');m=json.loads((Path(campaign)/chip/'tiles'/(tile+'.json')).read_text())['metadata'];objective=m.get('objective',{})
    if objective.get('magnification')!=20. or objective.get('na')!=.4 or m.get('white_level')!=4095.:
        raise ValueError('Unsupported optical profile: '+key+' requires recorded20x/NA0.4/white4095')
    return {'objective':objective,'white_level':m['white_level'],'camera_resources':m.get('camera_resources'),'camera_job_template':m.get('camera_job_template'),'illumination_percent':m.get('illumination_percent')}

def run(args):
    out=args.output.resolve();plan=json.loads((out/'plan.json').read_text());reserve=plan['reserve_gib'];output_guard(out,reserve)
    if digest(__file__)!=plan['supervisor_sha256']:raise ValueError('Supervisor changed since prepare')
    lock=(out/'offline-owner.lock').open('a+b');lock.seek(0);lock.write(b'1');lock.flush();lock.seek(0)
    import msvcrt
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:lock.close();raise RuntimeError('Another offline worker owns this output')
    started=time.time();state={'state':'running','processed_fields':0,'total_fields':len(plan['fields']),'counts':{},'sieve_failures':{},'updated_at':time.time(),'current_field':None,'source_failure_count':len(plan['source_failures']),'analysis_failure_count':0,'successful_fields':0,'excluded_fields':0,'unassessable_fields':0,'elapsed_seconds':0,'pid':os.getpid()}
    ledger={};counts=collections.Counter();failures=collections.Counter();consecutive_analysis_errors=0;survivors=[]
    source_failures=json.loads((out/'source-failures.json').read_text()) if (out/'source-failures.json').exists() else list(plan['source_failures'])
    analysis_failures=json.loads((out/'analysis-failures.json').read_text()) if (out/'analysis-failures.json').exists() else []
    state['historical_analysis_failure_count'] = len(analysis_failures)
    def check():
        if (out/'STOP').exists():raise StopRequested('Output STOP exists; never cleared automatically')
        if (PROJECT/'STOP').exists() or (Path(plan['workspace'])/'STOP').exists():
            raise StopRequested('Project STOP exists; never cleared automatically')
        output_guard(out,reserve)
    def status():
        state.update(updated_at=time.time(),elapsed_seconds=time.time()-started,counts=dict(counts),sieve_failures=dict(failures));save(out/'status.json',state)
        save(out/'catalog.json',{'schema':1,'state':state['state'],'updated_at':state['updated_at'],'processed_fields':state['processed_fields'],'total_fields':state['total_fields'],'counts':dict(counts),'rows':survivors,
            'scope':'Only current campaign shortlist/uncertain observations; all rejected and clipped observations remain in per-field JSONs.','no_material_identity_confirmed':True})
    try:
        verify_files(plan['source_hashes']);verify_files({str(out/k):v for k,v in plan['frozen_hashes'].items()})
        if (out/'ledger.json').is_file():ledger=json.loads((out/'ledger.json').read_text())
        sys.path.insert(0,str(out/'source/workspace'));sys.path.insert(0,str(out/'source'))
        spec=importlib.util.spec_from_file_location('frozen_sieve_worker',out/'source/run_sieves_batch_worker.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        worker.BATCH_CHECK=check
        for entry in plan['fields']:
            check();key=entry['field'];state['current_field']=key;status()
            verify_files(plan['source_hashes'])
            if key in ledger:
                committed=ledger[key];verify_files(entry['hashes']);verify_files(committed['hashes'])
            else:
                try:verify_files(entry['hashes']);optical_profile(plan['campaign'],key)
                except (ValueError,OSError) as exc:
                    source_failures.append({'field':key,'at':time.time(),'error':str(exc)});state['source_failure_count']+=1;save(out/'source-failures.json',source_failures);state['processed_fields']+=1;status();continue
                # Each attempt has a distinct directory; interrupted partial output is preserved.
                attempt=out/'field-attempts'/(key.replace('/','__')+'-'+uuid.uuid4().hex[:8]);check()
                worker.BATCH_INPUT=entry;old_argv=sys.argv
                sys.argv=['run_sieves_batch_worker.py','--workspace',str(out/'source/workspace'),'--campaign',plan['campaign'],'--output',str(attempt),'--fields',key]
                try:
                    worker.main();check();verify_files(entry['hashes'])
                    completion=json.loads((attempt/'completion.json').read_text());summary=completion['summary']
                    committed={'folder':str(attempt),'summary':{k:summary[k] for k in ('counts','sieve_failures','assessed_regions','elapsed_seconds')},'excluded':bool(completion['field_reports'] and completion['field_reports'][0].get('excluded')),
                        'hashes':{str(p):digest(p) for p in attempt.rglob('*') if p.is_file()}}
                    ledger[key]=committed;save(out/'ledger.json',ledger)
                except (StopRequested,StoragePause):raise
                except Exception as exc:
                    reason = quality_reason(exc)
                    if reason:
                        check();verify_files(entry['hashes'])
                        committed = record_unassessable(attempt, entry, exc, reason)
                        ledger[key] = committed;save(out/'ledger.json', ledger)
                    elif str(exc).startswith('Source hash mismatch:') or 'Source bytes differ' in str(exc):
                        source_failures.append({'field':key,'at':time.time(),'attempt':str(attempt),'error':str(exc)});state['source_failure_count']+=1;save(out/'source-failures.json',source_failures)
                    else:
                        analysis_failures.append({'field':key,'at':time.time(),'attempt':str(attempt),'error_type':type(exc).__name__,'error':str(exc)});state['analysis_failure_count']+=1;save(out/'analysis-failures.json',analysis_failures)
                        consecutive_analysis_errors+=1
                        if consecutive_analysis_errors>=3:
                            state['processed_fields']+=1
                            raise RuntimeError('Stopped after three consecutive analysis errors; inspect analysis-failures.json') from exc
                    if not reason:
                        state['processed_fields']+=1;status();continue
                finally:sys.argv=old_argv
            consecutive_analysis_errors=0
            counts.update(committed['summary']['counts']);failures.update(committed['summary']['sieve_failures']);state['processed_fields']+=1
            for field_json in sorted((Path(committed['folder'])/'fields').glob('*.json')):
                for row in json.loads(field_json.read_text())['rows']:
                    if row['disposition'] in ('shortlist','uncertain'):
                        record={k:row.get(k) for k in ('id','field','preview','source_shape_hw','bbox_px','polygons_px','area_um2','contrast_rgb','metrics','sieves','rank','disposition')}
                        record['source_field_reference']=field_json.relative_to(out).as_posix();survivors.append(record)
            state['excluded_fields' if committed['excluded'] else 'successful_fields']+=1;status()
            if committed.get('unassessable'):
                state['unassessable_fields'] += 1;status()
        state['state']='complete_with_failures' if state['analysis_failure_count'] or state['source_failure_count'] else ('complete_with_exclusions' if state['excluded_fields'] else 'complete')
    except StopRequested as exc:state.update(state='stopped',message=str(exc))
    except StoragePause as exc:state.update(state='storage_paused',message=str(exc))
    except Exception as exc:state.update(state='failed_closed',message=str(exc));raise
    finally:
        try:state['current_field']=None;status()
        finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1);lock.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['prepare','run']);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--workspace',type=Path);p.add_argument('--campaign',type=Path);p.add_argument('--reserve-gib',type=float,default=8.)
    a=p.parse_args()
    if a.mode=='prepare':
        if a.workspace is None or a.campaign is None:p.error('prepare requires --workspace and --campaign')
        if not 0<a.reserve_gib<1000:p.error('invalid reserve')
        prepare(a)
    else:run(a)

if __name__=='__main__':main()
