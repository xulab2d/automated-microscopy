"""Explicit local offload of verified OLD review/vendor copies; protected campaign stays."""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import bounded_transfer as transfer
import journal_transfer as journal_api


def allowed(relative, scope='review_vendor'):
    p=relative.replace('\\','/')
    if '..' in Path(p).parts or Path(p).is_absolute():
        return False
    if scope=='inactive_data':
        return p.startswith('data/') and not p.startswith('data/whole-chip-standardized-01/')
    old_analysis=p.startswith('data/chip-batch-01/') and '/analysis' in p and p.endswith(('.json','.npz'))
    if scope=='vendor':
        return p.startswith('data/zen-captures/')
    if scope=='old_analysis':
        return old_analysis and not p.endswith('/review-data.json')
    if scope=='all_eligible' and old_analysis:
        return True
    return p.startswith('data/zen-captures/') or (p.startswith('data/chip-batch-01/') and
        (p.endswith('/review.html') or p.endswith('/review-data.json') or '/review-assets/' in p))


def run(journal, review_files=300, include_initial_vendor=True, quiet_wait_seconds=300, scope='review_vendor',
        max_bytes=None, include_mounted=False, _connection=None):
    journal=Path(journal).resolve()
    owns_connection=_connection is None
    with transfer.ledger_lock(journal.parent/'worker-owner.json') if owns_connection else contextlib.nullcontext():
        connection=journal_api.connect(journal) if owns_connection else _connection
        try:
            info=journal_api.metadata(connection)
            root=Path(info['source_root']).resolve()
            drive=transfer.ensure_destination(info['destination_root'])
            protected=set()
            active_plan=drive/'data/whole-chip-standardized-01/analysis-residue-sieves-campaign-01/plan.json'
            if scope=='inactive_data':
                if not active_plan.is_file():raise ValueError('Active analysis protection plan unavailable.')
                plan=transfer.read_json(active_plan)
                for field in plan['fields']:
                    protected.update(str(Path(path).resolve()).lower() for path in field.get('hashes',{}))
                protected.update(str(Path(path).resolve()).lower() for path in plan.get('source_hashes',{}))
            run_id=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            folder=root/'research/drive-relocation/offload-state'/run_id
            candidates=[]
            states=('content_matched','client_confirmed','mounted_copy_verified' if include_mounted else 'client_confirmed')
            for sequence,record in connection.execute('SELECT sequence,record FROM files WHERE state IN (?,?,?) ORDER BY bytes DESC',states):
                row=json.loads(record)
                if allowed(row['relative_path'],scope) and str(Path(row['source']).resolve()).lower() not in protected and row.get('source_retained',True) and Path(row['source']).is_file():
                    candidates.append((sequence,row))
            candidates=candidates[:review_files]
            if include_initial_vendor:
                vendor_file=root/'research/drive-relocation/zen-transfer-ledger.json'
                vendor=transfer.read_json(vendor_file)
                for row in vendor['files']:
                    if row.get('state')!='client_confirmed' or not row['relative_path'].lower().endswith('.czi') or not Path(row['source']).is_file():
                        continue
                    relative=Path(row['source']).resolve().relative_to(root).as_posix()
                    old=connection.execute('SELECT sequence,record FROM files WHERE relative_path=?',(relative,)).fetchone()
                    if old is None:raise ValueError('Separate vendor receipt has no matching main inventory.')
                    old_record=json.loads(old['record'])
                    if old_record['destination']!=row['destination'] or old_record['bytes']!=row['bytes']:
                        raise ValueError('Vendor receipt differs from main mapping.')
                    candidates.append((old['sequence'],{**row,'relative_path':relative,'separate_prior_ledger':str(vendor_file)}))
            candidates=list({seq:(seq,row) for seq,row in candidates}.values())
            if max_bytes is not None:
                bounded=[];size=0
                for sequence,row in candidates:
                    if size+row['bytes']<=max_bytes:
                        bounded.append((sequence,row));size+=row['bytes']
                candidates=bounded
            if not candidates:
                return {'state':'no_eligible_verified_copies','files_removed':0,'bytes_removed':0}
            folder.mkdir(parents=True,exist_ok=False)
            prepared=[]
            for sequence,row in candidates:
                source=Path(row['source']).resolve(strict=True)
                relative=source.relative_to(root).as_posix()
                if relative!=row['relative_path'] or not allowed(relative,scope) or str(source).lower() in protected or source.is_symlink() or getattr(source.lstat(),'st_file_attributes',0)&1024:
                    raise ValueError('Protected/ambiguous source path: '+relative)
                if Path(row['destination']).resolve()!=drive/Path(relative):
                    raise ValueError('Destination mapping does not match source.')
                hashes=transfer.probe.hashes(source)
                if not (hashes['sha256']==row.get('sha256')==row.get('mounted_sha256') and hashes['md5']==row.get('md5') and hashes['bytes']==row['bytes']):
                    raise ValueError('Current source differs from its completed readback receipt.')
                prepared.append((sequence,row,hashes))
            rows=[row for _,row,_ in prepared]
            started=time.monotonic()
            while True:
                if (root/'research/drive-relocation/STOP').exists():
                    raise transfer.RelocationStopped('STOP before uploaded-copy offload.')
                first=transfer.probe.metadata_snapshots(rows)
                time.sleep(10)
                second=transfer.probe.metadata_snapshots(rows)
                gates=[transfer.client_gate(a,b,row) for a,b,row in zip(first,second,rows)]
                if all(gate['client_confirmed'] for gate in gates):break
                if time.monotonic()-started>=quiet_wait_seconds:
                    result={'state':'blocked_verification','files_deleted':0,'queue':max((s.get('global_pending_operations',0) for s in second),default=0)}
                    transfer.write_json(folder/'result.json',result)
                    return result
                print(json.dumps({'state':'waiting_for_quiet_verified_snapshots','pending_operations':max((s.get('global_pending_operations',0) for s in second),default=0)}),flush=True)
                time.sleep(10)
            proofs=[{'sequence':seq,'row':row,'current_source_hashes':hashes,'client_confirmation':gate,'metadata_snapshots':[a,b]}
                for (seq,row,hashes),gate,a,b in zip(prepared,gates,first,second)]
            proof_path=folder/'verified-removal-plan.json'
            plan={'schema':1,'run_id':run_id,'authorization':'User authorized routine removal of verified uploaded local data. Scope is explicit; all current whole-chip inputs, code/configs and active-plan dependencies remain protected.',
                'scope':scope,'active_plan':str(active_plan) if scope=='inactive_data' else None,
                'active_plan_sha256':hashlib.sha256(active_plan.read_bytes()).hexdigest() if scope=='inactive_data' else None,
                'files':proofs,'planned_files':len(proofs),'planned_bytes':sum(p['row']['bytes'] for p in proofs),
                'prepared_at_utc':transfer.probe.now(),'free_before_bytes':shutil.disk_usage(root.anchor).free,'source_deletion_performed':False}
            transfer.write_json(proof_path,plan)
            cloud_proof=drive/'transfer-evidence/local-offload'/run_id/'verified-removal-plan.json'
            cloud_proof.parent.mkdir(parents=True,exist_ok=False)
            payload=proof_path.read_bytes()
            with cloud_proof.open('xb') as stream:stream.write(payload)
            if hashlib.sha256(cloud_proof.read_bytes()).digest()!=hashlib.sha256(payload).digest():
                raise ValueError('Removal plan readback failed; no source deletion.')
            removed=[]
            log=folder/'removals.jsonl'
            with log.open('x',encoding='utf-8') as stream:
                for proof in proofs:
                    if (root/'research/drive-relocation/STOP').exists():
                        raise transfer.RelocationStopped('STOP during uploaded-copy offload; completed receipts retained.')
                    sequence=proof['sequence'];row=proof['row']
                    source=Path(row['source']).resolve(strict=True)
                    hashes=transfer.probe.hashes(source)
                    if hashes['sha256']!=proof['current_source_hashes']['sha256'] or hashes['bytes']!=row['bytes']:
                        raise ValueError('Source changed since removal plan; remaining removals stopped.')
                    old=json.loads(connection.execute('SELECT record FROM files WHERE sequence=?',(sequence,)).fetchone()[0])
                    updated={**old,**row,'state':'client_confirmed','client_confirmation':proof['client_confirmation'],'source_retained':True,
                        'source_offload':{'state':'prepared','proof':str(proof_path),'cloud_proof':str(cloud_proof),'sha256':row['sha256'],'prepared_at_utc':transfer.probe.now()}}
                    journal_api.store_file(connection,sequence,updated)
                    source.unlink()
                    receipt={'relative_path':row['relative_path'],'absolute_path':str(source),'bytes':row['bytes'],'sha256':row['sha256'],
                        'removed_at_utc':transfer.probe.now(),'cloud_destination':row['destination'],'cloud_id':proof['client_confirmation']['cloud_id'],'proof':str(proof_path)}
                    stream.write(json.dumps(receipt)+'\n');stream.flush();os.fsync(stream.fileno())
                    updated['source_retained']=False
                    updated['source_offload'].update(state='completed',removed_at_utc=receipt['removed_at_utc'],removal_journal=str(log))
                    journal_api.store_file(connection,sequence,updated)
                    removed.append(receipt)
            result={'state':'completed','run_id':run_id,'files_removed':len(removed),'bytes_removed':sum(row['bytes'] for row in removed),
                'free_after_bytes':shutil.disk_usage(root.anchor).free,'whole_chip_sources_removed':False,'cloud_files_removed':False,
                'proof':str(proof_path),'removal_journal':str(log),'journal_reconciled':True,'transfer_started':False}
            transfer.write_json(folder/'result.json',result)
            transfer.write_json(cloud_proof.parent/'result.json',result)
            return result
        finally:
            if owns_connection:connection.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',required=True)
    parser.add_argument('--max-files',type=int,default=300)
    parser.add_argument('--no-initial-vendor',action='store_true')
    parser.add_argument('--quiet-wait-seconds',type=float,default=300)
    parser.add_argument('--scope',choices=['review_vendor','old_analysis','all_eligible','vendor','inactive_data'],default='review_vendor')
    args=parser.parse_args()
    print(json.dumps(run(args.journal,args.max_files,not args.no_initial_vendor,args.quiet_wait_seconds,args.scope),indent=2))
