"""Supervise a copy-only mirror, then reconcile frozen source revisions.

No hardware, original deletion, Drive database/cache writes, or silent overwrite.
The running worker owns its existing lock. This supervisor waits for release,
never kills it, and stops truthfully on storage, upload, or conflict failures.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

import bounded_transfer as transfer
import journal_transfer as journal_api
import refresh_canonical
import refresh_plan


def owner_active(journal):
    try:
        with transfer.ledger_lock(Path(journal).parent/'worker-owner.json'):
            return False
    except OSError:
        return True


def reconcile_verified(journal, ledger):
    """Install a proven canonical revision in OUR journal, preserving lineage."""
    data = transfer.read_json(ledger)
    transfer.validate_ledger(data)
    if any(row.get('state') != 'client_confirmed' or not row.get('client_confirmation', {}).get('client_confirmed')
           for row in data['files']):
        raise ValueError('Cannot reconcile a revision without client confirmation.')
    with transfer.ledger_lock(Path(journal).parent/'worker-owner.json'):
        connection = journal_api.connect(journal)
        try:
            info = journal_api.metadata(connection)
            if (Path(info['source_root']).resolve(), Path(info['destination_root']).resolve()) != (
                    Path(data['source_root']).resolve(), Path(data['destination_root']).resolve()):
                raise ValueError('Revision roots differ from mirror roots.')
            next_sequence = connection.execute('SELECT coalesce(min(sequence),0)-1 FROM files').fetchone()[0]
            with connection:
                for row in data['files']:
                    actual = transfer.probe.hashes(row['destination'])
                    if actual['sha256'] != row['sha256']:
                        raise ValueError('Canonical destination differs from verified revision: '+row['relative_path'])
                    old = connection.execute('SELECT * FROM files WHERE relative_path=?', (row['relative_path'],)).fetchone()
                    record = dict(row)
                    if old:
                        previous = json.loads(old['record'])
                        history = previous.get('revision_history', [])
                        if previous.get('sha256') != row['sha256'] or previous.get('source_mtime_ns') != row['source_mtime_ns']:
                            previous = {k:v for k,v in previous.items() if k!='revision_history'}
                            history = history+[{'previous_receipt': previous, 'replaced_by_ledger': str(Path(ledger).resolve()),
                                'reconciled_at_utc': transfer.probe.now()}]
                        record['revision_history'] = history
                        connection.execute('UPDATE files SET state=?,bytes=?,record=? WHERE sequence=?',
                            ('client_confirmed',row['bytes'],json.dumps(record),old['sequence']))
                    else:
                        connection.execute('INSERT INTO files VALUES(?,?,?,?,?,?)',
                            (next_sequence,row['relative_path'],'client_confirmed',row['bytes'],'0_verified_revision',json.dumps(record)))
                        next_sequence -= 1
            return len(data['files'])
        finally:
            connection.close()


def delta_inventory(plan, output):
    rows = [{**row, 'state':'pending'} for row in plan['new_files']]
    data = {'schema':1, 'mode':'copy_only', 'created_at_utc':transfer.probe.now(),
        'source_root':plan['source_root'], 'destination_root':plan['destination_root'],
        'files':rows,'file_count':len(rows),'total_bytes':sum(row['bytes'] for row in rows),
        'mapping_sha256':transfer.mapping_digest(rows), 'batches':[], 'active_batch':None,
        'max_batch_bytes':256*transfer.MIB,'reserve_bytes':8*transfer.GIB,
        'max_batch_files':4096,'allow_single_oversized_file':True,'source_deletion_performed':False}
    transfer.write_json(output,data)
    return output


def verify_until(ledger, check_cancel, timeout_minutes=120):
    started=time.monotonic()
    while True:
        check_cancel()
        result=transfer.verify_batch(ledger)
        if result['state']=='client_confirmed':
            return result
        if time.monotonic()-started >= timeout_minutes*60:
            raise TimeoutError('Upload confirmation did not complete within bounded wait: '+str(ledger))
        time.sleep(30)


def freeze_ready(path, source_root):
    if not Path(path).exists():
        return False
    data=transfer.read_json(path)
    if data.get('schema')!=1 or data.get('ready') is not True or not data.get('frozen_at_utc'):
        raise ValueError('Invalid source-freeze marker.')
    if data.get('source_root') and Path(data['source_root']).resolve()!=Path(source_root).resolve():
        raise ValueError('Source-freeze root mismatch.')
    return True


def run(journal, stop_file, freeze_file, folder, adopt_refresh=(), max_cycles=5):
    journal, stop_file, freeze_file, folder=map(lambda p:Path(p).resolve(),(journal,stop_file,freeze_file,folder))
    folder.mkdir(parents=True,exist_ok=True)
    with transfer.ledger_lock(folder/'supervisor-owner.json'):
        connection=journal_api.connect(journal)
        info=journal_api.metadata(connection)
        connection.close()
        def state(value,**details):
            connection=journal_api.connect(journal)
            offloaded=connection.execute("SELECT count(*),coalesce(sum(bytes),0) FROM files WHERE json_extract(record,'$.source_offload.state')='completed' AND json_extract(record,'$.source_retained')=0").fetchone()
            retired=journal_api.summary(connection).get('retired_intentionally',{'files':0,'bytes':0})
            connection.close()
            output={'schema':1,'state':value,'pid':os.getpid(),'updated_at_utc':transfer.probe.now(),
                'journal':str(journal),'freeze_file':str(freeze_file),'source_deletion_performed':bool(offloaded[0] or retired['files']),
                'source_originals_retained':not bool(offloaded[0] or retired['files']),'all_source_originals_retained':not bool(offloaded[0] or retired['files']),
                'local_copies_offloaded':{'files':offloaded[0],'bytes':offloaded[1]},
                'intentionally_retired_intermediates':retired,
                'all_cloud_copies_preserved':not bool(retired['files']),'retained_scope_cloud_copies_preserved':True,'current_final_scientific_inputs_preserved':True,**details}
            transfer.write_json(folder/'supervisor-state.json',output)
            print(json.dumps(output),flush=True)
            if value not in ('waiting_for_worker','waiting_for_source_freeze'):
                transfer.write_json(Path(info['destination_root'])/'TRANSFER-COMPLETION.json',output)
            if value.startswith('blocked') or value in ('complete','stopped'):
                from publish_transfer_status import publish_if_changed
                connection=journal_api.connect(journal)
                progress=journal_api.summary(connection)
                connection.close()
                publish_if_changed(info,journal.parent,{**output,'progress':progress},force=True)
        def cancel():
            if stop_file.exists():
                raise transfer.RelocationStopped('Relocation STOP requested; all originals retained.')
        try:
            state('waiting_for_worker')
            while owner_active(journal):
                cancel()
                time.sleep(30)
            cancel()
            worker=transfer.read_json(journal.parent/'worker-state.json')
            if worker['state'] not in ('complete','complete_refresh_required'):
                state('blocked_prior_worker',worker_state=worker['state'],error=worker.get('error'),
                    action='Resolve the recorded storage/upload/error condition; no automatic deletion or repeated retries.')
                return 2
            state('waiting_for_source_freeze')
            while not freeze_ready(freeze_file,info['source_root']):
                cancel()
                time.sleep(30)
            # Reconcile previously explicit canonical updates before choosing old hashes.
            for ledger in adopt_refresh:
                state('verifying_prior_revision',ledger=str(ledger))
                verify_until(ledger,cancel)
                reconcile_verified(journal,ledger)
            for cycle in range(max_cycles):
                cancel()
                cycle_folder=folder/f'cycle-{cycle+1:02d}'
                cycle_folder.mkdir(exist_ok=True)
                proposal=cycle_folder/'refresh-proposal.json'
                state('auditing_frozen_sources',cycle=cycle+1)
                counts=refresh_plan.audit(journal,proposal)
                plan=transfer.read_json(proposal)
                if plan['reparse_paths_deferred'] or plan['in_progress_files_deferred']:
                    raise ValueError('Source audit contains unresolved deferred/reparse paths.')
                connection=journal_api.connect(journal)
                progress=journal_api.summary(connection)
                connection.close()
                if progress.get('source_missing',{}).get('files',0):
                    raise ValueError('Original inventory has missing sources; cannot claim complete.')
                if not counts['new_files'] and not counts['changed_files']:
                    if set(progress)-{'client_confirmed','retired_intentionally'}:
                        raise ValueError('Unresolved journal states remain after source audit: '+str(progress))
                    state('exporting_final_evidence',progress=progress)
                    export_evidence(journal,folder,info,cancel)
                    state('complete',progress=progress,qualification='All retained-scope current source versions are client-confirmed. Explicitly authorized intermediate discards are retired separately, not counted as uploaded. Eligible uploaded local copies may be offloaded with per-file receipts; current final scientific inputs remain protected. Runtime environments, credentials, caches and live journals are excluded; journal evidence exported separately.')
                    return 0
                if counts['new_files']:
                    inventory=delta_inventory(plan,cycle_folder/'delta-inventory.json')
                    journal_api.append_inventory(journal,inventory)
                    state('copying_new_files',files=counts['new_files'],bytes=counts['new_bytes'])
                    result=journal_api.run_pipeline(journal,stop_file)
                    if result not in (0,4):
                        state('blocked_refresh_copy',worker_exit=result)
                        return result
                if counts['changed_files']:
                    # New files were handled by the bounded worker, not a giant refresh batch.
                    plan['new_files']=[]
                    transfer.write_json(cycle_folder/'changed-only.json',plan)
                    state('refreshing_canonical_revisions',files=counts['changed_files'])
                    result=refresh_canonical.refresh(journal,cycle_folder/'changed-only.json',cycle_folder/'revisions')
                    if result['conflicts']:
                        state('blocked_revision_conflict',result=result)
                        return 5
                    verify_until(result['ledger'],cancel)
                    reconcile_verified(journal,result['ledger'])
            state('blocked_sources_keep_changing',cycles=max_cycles)
            return 6
        except transfer.RelocationStopped as exc:
            state('stopped',error=str(exc))
            return 0
        except Exception as exc:
            state('blocked',error=repr(exc))
            return 1


def export_evidence(journal,folder,info,cancel):
    """Consistent SQLite backup; never copy a live WAL as if it were complete."""
    evidence=folder/'final-evidence'
    evidence.mkdir(exist_ok=True)
    backup=evidence/'progress.sqlite'
    with transfer.ledger_lock(journal.parent/'worker-owner.json'):
        source=sqlite3.connect(journal)
        target=sqlite3.connect(backup)
        source.backup(target)
        target.close()
        source.close()
    sources=[backup,Path(info['inventory_file'])]
    rows=[]
    for source in sources:
        cancel()
        hashes=transfer.probe.hashes(source)
        destination=Path(info['destination_root'])/'transfer-evidence'/(source.stem+'-'+hashes['sha256'][:16]+source.suffix)
        refresh_canonical.exact_copy(source,destination,hashes['sha256'])
        rows.append({'relative_path':source.name,'source':str(source),'destination':str(destination),
            **hashes,'source_mtime_ns':hashes['mtime_ns'],'state':'mounted_copy_verified',
            'mounted_sha256':hashes['sha256'],'source_retained':True})
    # These evidence files have explicit mappings and their own two-snapshot proof.
    first=transfer.probe.metadata_snapshots(rows)
    started=time.monotonic()
    while True:
        cancel()
        time.sleep(10)
        second=transfer.probe.metadata_snapshots(rows)
        gates=[transfer.client_gate(a,b,row) for a,b,row in zip(first,second,rows)]
        if all(g['client_confirmed'] for g in gates):
            transfer.write_json(evidence/'confirmation.json',{'files':[dict(row,client_confirmation=gate)
                for row,gate in zip(rows,gates)],'source_deletion_performed':False})
            return
        if time.monotonic()-started>7200:
            raise TimeoutError('Final evidence upload not confirmed within two hours.')
        first=second
        time.sleep(20)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',required=True)
    parser.add_argument('--stop-file',required=True)
    parser.add_argument('--freeze-file',required=True)
    parser.add_argument('--output-folder',required=True)
    parser.add_argument('--adopt-refresh',action='append',default=[])
    args=parser.parse_args()
    raise SystemExit(run(args.journal,args.stop_file,args.freeze_file,args.output_folder,args.adopt_refresh))
