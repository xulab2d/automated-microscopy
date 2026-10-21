"""Opt-in bounded field processes; only the parent commits the analysis ledger."""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import gc,time,uuid
import analyze_graphene_run as driver

MEASURED_WORKER_PEAK_BYTES=531*1024**2


def process_field(entry,model,attempt,args,output,cpu_mask):
    driver.cpu_policy(cpu_mask=cpu_mask);started=time.monotonic()
    def check():
        if (output/'STOP').exists():raise driver.watcher.AnalysisStop('Independent offline-analysis STOP requested.')
        if time.monotonic()-started>args.field_timeout:raise driver.watcher.FieldBudget('Offline field time budget exceeded.')
    try:payload=driver.analyze_field(entry,model,attempt,args,check)
    except (driver.watcher.AnalysisStop,MemoryError):raise
    except Exception as exc:
        attempt.mkdir(parents=True,exist_ok=True)
        payload={'assessment_state':driver.assessment_failure(exc),'field':entry['field'],'reason':str(exc),
            'exception_type':type(exc).__name__,'source_hashes':entry['source_hashes'],
            'source_features_not_disproved':True,'regions':None,'memory_at_failure':driver.memory_snapshot()}
        driver.watcher.save(attempt/'field.json',payload)
    check()
    return {'state':payload['assessment_state'],'reason':payload.get('reason'),
        'field_json':(attempt/'field.json').relative_to(output).as_posix(),
        'regions':len(payload['regions']) if payload.get('regions') is not None else None,
        'hashes':{p.relative_to(output).as_posix():driver.digest(p,check) for p in attempt.iterdir() if p.is_file()}}


def run_fields(tiles,chip,manifest,model,args,output,ledger,state,*,check,status,verify_sources,cpu_masks,executor_type=ProcessPoolExecutor):
    from flakefinder.memory_capacity import snapshot,assess
    pending=[]
    for tile in tiles:
        if tile in ledger:
            for name,expected in ledger[tile]['hashes'].items():
                if driver.digest(output/name,check)!=expected:raise ValueError('Previously committed output changed: '+name)
        else:pending.append(tile)
    with executor_type(max_workers=2) as pool:
        for start in range(0,len(pending),2):
            check()
            while (output/'PAUSE').exists():
                state.update(state='paused_for_quiet_window',current_field=None);status();check();time.sleep(1)
            capacity=assess(snapshot(),2*MEASURED_WORKER_PEAK_BYTES,purpose='two measured offline segmentation workers')
            state['memory_capacity']=capacity
            if not capacity['adequate']:
                state.update(state='resource_paused',reason='Measured worker allocations exceed available commit/address capacity.');return
            verify_sources();batch=pending[start:start+2]
            state.update(state='running',current_field=','.join(batch),finished_fields=len(ledger),selected_fields=len(tiles));status()
            futures=[]
            for slot,tile in enumerate(batch):
                entry=driver.field_entry(chip,tile,manifest);attempt=output/'fields'/(tile+'-'+uuid.uuid4().hex[:8])
                futures.append((tile,pool.submit(process_field,entry,model,attempt,args,output,cpu_masks[slot])))
            # At most two in flight; no image arrays or region payloads cross IPC.
            for tile,future in futures:
                try:record=future.result()
                except MemoryError:
                    state.update(state='resource_paused',reason='Worker allocation failed; incomplete output is uncommitted.');return
                check();verify_sources();ledger[tile]=record
                driver.watcher.save(output/'ledger.json',ledger)
                state.update(finished_fields=len(ledger));status();gc.collect()
    state.update(state='complete',current_field=None)
