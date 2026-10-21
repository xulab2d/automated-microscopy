"""Bounded COPY-ONLY helper and read-only DriveFS evidence collector.

Never deletes source data, evicts caches, changes Drive settings, reads tokens,
or writes a Drive database. Cached metadata is not treated as server proof.
"""
from pathlib import Path
import argparse,datetime,hashlib,json,os,re,shutil,sqlite3,struct,time

DB=Path(r'C:\Users\STM\AppData\Local\Google\DriveFS\102154920943365146603\metadata_sqlite_db')
SHARED=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments')
SHARED_ID='0AGMvZpRbSaSKUk9PVA'
GIB=1024**3

def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def hashes(path):
    path=Path(path);before=path.stat();sha=hashlib.sha256();md5=hashlib.md5()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):sha.update(block);md5.update(block)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Source changed while hashing.')
    return {'bytes':after.st_size,'mtime_ns':after.st_mtime_ns,'sha256':sha.hexdigest(),'md5':md5.hexdigest()}

def varint(data,pos):
    value=0;shift=0
    while pos<len(data) and shift<70:
        byte=data[pos];pos+=1;value|=(byte&127)<<shift
        if byte<128:return value,pos
        shift+=7
    raise ValueError('Invalid protobuf varint.')

def protobuf_fields(data):
    """Parse wire framing only. No undocumented field meanings are asserted."""
    pos=0;out=[]
    while pos<len(data):
        tag,pos=varint(data,pos);field,wire=tag>>3,tag&7
        if not field:raise ValueError('Invalid field zero.')
        if wire==0:value,pos=varint(data,pos)
        elif wire in (1,5):
            size=8 if wire==1 else 4;value=data[pos:pos+size];pos+=size
        elif wire==2:
            size,pos=varint(data,pos);value=data[pos:pos+size];pos+=size
            if len(value)!=size:raise ValueError('Truncated protobuf field.')
        else:raise ValueError('Unsupported protobuf wire type.')
        out.append((field,wire,value))
    return out

def checksum_paths(data,expected,path='',depth=0):
    found=[]
    try:fields=protobuf_fields(data)
    except ValueError:return []
    for field,wire,value in fields:
        key=path+str(field)
        if wire!=2:continue
        if value==expected.encode() or value==bytes.fromhex(expected):found.append(key)
        elif depth<5:found+=checksum_paths(value,expected,key+'.',depth+1)
    return found

def metadata_snapshot(destination,expected,db=DB,shared=SHARED):
    destination=Path(destination).resolve()
    relative=destination.relative_to(Path(shared).resolve())
    connection=sqlite3.connect(Path(db).as_uri()+'?mode=ro',uri=True,timeout=2)
    connection.execute('PRAGMA query_only=ON');connection.row_factory=sqlite3.Row
    try:
        connection.execute('BEGIN')
        root=connection.execute('SELECT stable_id FROM items WHERE id=? AND is_folder=1 AND trashed=0',(SHARED_ID,)).fetchall()
        if len(root)!=1:raise ValueError('Exact shared drive is absent or ambiguous in metadata.')
        parent=root[0]['stable_id'];ancestry=[];item=None
        for component in relative.parts:
            rows=connection.execute('''SELECT i.stable_id,i.id,i.local_title,i.file_size,i.is_folder,i.trashed,
                i.is_tombstone,i.team_drive_stable_id,s.cloud_id,
                EXISTS(SELECT 1 FROM local_stable_ids l WHERE l.stable_id=i.stable_id) AS local_only,
                CAST(i.proto AS BLOB) AS metadata_blob
                FROM items i JOIN stable_parents p ON p.item_stable_id=i.stable_id
                LEFT JOIN stable_ids s ON s.stable_id=i.stable_id
                WHERE p.parent_stable_id=? AND i.local_title=? AND i.trashed=0 AND i.is_tombstone=0''',(parent,component)).fetchall()
            if not rows:return {'observed_at_utc':now(),'status':'not_yet_in_metadata','missing_component':component,'cloud_completion_verified':False}
            if len(rows)!=1:raise ValueError('Ambiguous destination path in Drive metadata.')
            item=dict(rows[0]);blob=item.pop('metadata_blob');parent=item['stable_id'];ancestry.append({k:item[k] for k in ('local_title','stable_id','cloud_id')})
        if item is None:raise ValueError('Expected a file inside the shared drive.')
        md5_paths=checksum_paths(blob,expected['md5'])
        checksum_values=[value.decode('ascii').lower() for field,wire,value in protobuf_fields(blob)
            if field==48 and wire==2 and re.fullmatch(rb'[0-9a-fA-F]{32}',value)]
        cached_md5=checksum_values[0] if len(checksum_values)==1 else None
        pending=connection.execute('SELECT count(*) FROM operations').fetchone()[0]
        return {'observed_at_utc':now(),'status':'cached_metadata_observed','item':item,'ancestry':ancestry,
          'shared_drive_id':SHARED_ID,'cached_metadata_bytes':len(blob),'cached_metadata_sha256':hashlib.sha256(blob).hexdigest(),
          'cached_md5_field48':cached_md5,'cached_md5_matches':cached_md5==expected['md5'].lower(),
          'source_md5_matches_cached_metadata':bool(md5_paths),'matching_undocumented_field_paths':md5_paths,
          'size_matches':item['file_size']==expected['bytes'],'global_pending_operations':pending,
          'cloud_completion_verified':False,
          'qualification':'Undocumented cached metadata and queue state are corroboration only; not an independent server checksum response.'}
    finally:connection.close()

def relevant_log_events(snapshot,destination):
    item=snapshot.get('item',{});cloud_id=item.get('cloud_id');name=Path(destination).name
    if not cloud_id:return []
    needles=(cloud_id.encode(),name.encode());result=[]
    # Read the current account log only. Do not dump unrelated global UI records.
    log=DB.parent.parent/'Logs'/('structured_log_'+DB.parent.name)
    if not log.exists():return []
    with log.open('rb') as stream:
        while True:
            offset=stream.tell();header=stream.read(4)
            if len(header)!=4:break
            length=struct.unpack('<I',header)[0]
            if length>1024*1024:break
            record=stream.read(length)
            if len(record)!=length:break
            if not any(needle in record for needle in needles):continue
            try:fields=protobuf_fields(record)
            except ValueError:continue
            scalars={field:value for field,wire,value in fields if wire==0}
            result.append({'log':str(log),'offset':offset,'record_sha256':hashlib.sha256(record).hexdigest(),
              'event_code_undocumented':scalars.get(1),'timestamp_microseconds':scalars.get(2),
              'field13_undocumented':scalars.get(13),'field14_undocumented':scalars.get(14),
              'mentions_exact_cloud_id':cloud_id.encode() in record,'mentions_exact_name':name.encode() in record,
              'interpretation':'Field semantics not verified; do not equate numeric status0 with upload success.'})
    return result[-30:]


def metadata_snapshots(entries,db=DB,shared=SHARED):
    """One read-only transaction with cached ancestor lookups for an entire batch."""
    connection=sqlite3.connect(Path(db).as_uri()+'?mode=ro',uri=True,timeout=2)
    connection.execute('PRAGMA query_only=ON');connection.row_factory=sqlite3.Row
    shared=Path(shared).resolve()
    try:
        connection.execute('BEGIN')
        roots=connection.execute('SELECT stable_id FROM items WHERE id=? AND is_folder=1 AND trashed=0',(SHARED_ID,)).fetchall()
        if len(roots)!=1:raise ValueError('Exact shared drive is absent or ambiguous.')
        root=roots[0]['stable_id'];cache={};result=[]
        pending=connection.execute('SELECT count(*) FROM operations').fetchone()[0]
        observed=now()
        for expected in entries:
            relative=Path(os.path.abspath(expected['destination'])).relative_to(shared)
            parent=root;ancestry=[];item=None;blob=b'';missing=None
            for component in relative.parts:
                key=(parent,component)
                if key not in cache:
                    rows=connection.execute('''SELECT i.stable_id,i.id,i.local_title,i.file_size,i.is_folder,i.trashed,
                        i.is_tombstone,i.team_drive_stable_id,s.cloud_id,
                        EXISTS(SELECT 1 FROM local_stable_ids l WHERE l.stable_id=i.stable_id) AS local_only,
                        CAST(i.proto AS BLOB) AS metadata_blob
                        FROM items i JOIN stable_parents p ON p.item_stable_id=i.stable_id
                        LEFT JOIN stable_ids s ON s.stable_id=i.stable_id
                        WHERE p.parent_stable_id=? AND i.local_title=? AND i.trashed=0 AND i.is_tombstone=0''',key).fetchall()
                    if len(rows)>1:raise ValueError('Ambiguous destination path in metadata.')
                    cache[key]=dict(rows[0]) if rows else None
                value=cache[key]
                if value is None:
                    missing=component;break
                item=dict(value);blob=item.pop('metadata_blob');parent=item['stable_id']
                ancestry.append({k:item[k] for k in ('local_title','stable_id','cloud_id')})
            if missing:
                result.append({'observed_at_utc':observed,'status':'not_yet_in_metadata',
                    'missing_component':missing,'cloud_completion_verified':False})
                continue
            if item is None:raise ValueError('Expected a file within shared drive.')
            checksum_values=[value.decode('ascii').lower() for field,wire,value in protobuf_fields(blob)
                if field==48 and wire==2 and re.fullmatch(rb'[0-9a-fA-F]{32}',value)]
            cached_md5=checksum_values[0] if len(checksum_values)==1 else None
            result.append({'observed_at_utc':observed,'status':'cached_metadata_observed','item':item,
                'ancestry':ancestry,'shared_drive_id':SHARED_ID,
                'cached_metadata_bytes':len(blob),'cached_metadata_sha256':hashlib.sha256(blob).hexdigest(),
                'cached_md5_field48':cached_md5,'cached_md5_matches':cached_md5==expected['md5'],
                'size_matches':item['file_size']==expected['bytes'],'global_pending_operations':pending,
                'cloud_completion_verified':False,
                'qualification':'Read-only cached client metadata; undocumented field48 MD5 semantics are empirical, not an independent server response.'})
        return result
    finally:connection.close()

def compare_server_attestation(path,expected,snapshot):
    """Compare a separately obtained Drive API response or web-download receipt.

    Does not obtain authentication or bless the receipt's acquisition method.
    The transfer owner must collect it through the authenticated API/browser.
    """
    if path is None:return {'status':'missing_independent_server_evidence','verified':False}
    attestation=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    cloud_id=snapshot.get('item',{}).get('cloud_id')
    if attestation.get('method')=='drive_api_files_get':
        remote=attestation['file'];checks={'file_id':remote.get('id')==cloud_id,
          'shared_drive':remote.get('driveId')==SHARED_ID,'size':int(remote.get('size',-1))==expected['bytes'],
          'not_trashed':remote.get('trashed') is False,
          'checksum':remote.get('sha256Checksum','').lower()==expected['sha256'] or remote.get('md5Checksum','').lower()==expected['md5']}
    elif attestation.get('method')=='authenticated_drive_web_download':
        checks={'file_id':attestation.get('file_id')==cloud_id,'size':attestation.get('bytes')==expected['bytes'],
          'sha256':attestation.get('sha256','').lower()==expected['sha256'],
          'independent_network_download':attestation.get('downloaded_from_google_server') is True}
    else:return {'status':'unsupported_attestation_method','verified':False}
    return {'status':'server_attestation_matches' if all(checks.values()) else 'server_attestation_mismatch',
      'verified':all(checks.values()),'checks':checks,'attestation_file':str(Path(path).resolve()),
      'attestation_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
      'qualification':'Receipt content matches; caller remains responsible for collecting authentic, current server evidence.'}

def probe(source,destination,attestation=None):
    expected=hashes(source);snapshot=metadata_snapshot(destination,expected)
    return {'source':str(Path(source).resolve()),'destination':str(destination),'source_hashes':expected,
      'read_only_drivefs_evidence':snapshot,'relevant_account_log_events':relevant_log_events(snapshot,destination),
      'independent_server_evidence':compare_server_attestation(attestation,expected,snapshot),
      'source_deletion_performed':False,'free_cache_volume_bytes':shutil.disk_usage(DB.anchor).free,'observed_at_utc':now()}

def copy_batch(plan_path,report_path,max_bytes=256*1024**2,reserve_bytes=8*GIB):
    """Copy explicit files only. No cleanup/removal, including partial outputs."""
    plan=json.loads(Path(plan_path).read_text(encoding='utf-8-sig'));entries=plan['files']
    sizes=[Path(entry['source']).stat().st_size for entry in entries]
    if sum(sizes)>max_bytes:raise ValueError('Plan exceeds explicit bounded batch allowance.')
    if shutil.disk_usage(DB.anchor).free<reserve_bytes+2*sum(sizes):raise OSError('Insufficient C: reserve for this batch.')
    report={'schema':1,'mode':'copy_only','started_at_utc':now(),'reserve_bytes':reserve_bytes,'max_batch_bytes':max_bytes,'files':[]}
    def save():Path(report_path).write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    for entry in entries:
        source=Path(entry['source']).resolve();destination=Path(entry['destination'])
        destination.relative_to(SHARED)
        if source.is_relative_to(DB.parent.parent):raise ValueError('DriveFS cache/credential material must never enter relocation plans.')
        before=source.stat();record={'source':str(source),'destination':str(destination),'bytes':before.st_size,'state':'copy_started'}
        report['files'].append(record);save()
        try:
            if destination.exists():raise FileExistsError('Refusing to overwrite an existing destination.')
            if shutil.disk_usage(DB.anchor).free<reserve_bytes+2*before.st_size:raise OSError('C: reserve guard before copy.')
            destination.parent.mkdir(parents=True,exist_ok=True);sha=hashlib.sha256();md5=hashlib.md5()
            with source.open('rb') as src,destination.open('xb') as dst:
                while True:
                    block=src.read(1024*1024)
                    if not block:break
                    if shutil.disk_usage(DB.anchor).free<reserve_bytes+4*1024**2:raise OSError('C: reserve guard during copy; partial file retained.')
                    dst.write(block);sha.update(block);md5.update(block)
                dst.flush();os.fsync(dst.fileno())
            after=source.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Source changed during copy; retain both and stop.')
            copied=hashes(destination)
            if copied['sha256']!=sha.hexdigest() or copied['bytes']!=before.st_size:raise ValueError('Mounted destination readback differs.')
            record.update(state='mounted_copy_verified_cloud_unverified',sha256=sha.hexdigest(),md5=md5.hexdigest(),source_mtime_ns=after.st_mtime_ns,
              source_retained=True,server_verified=False,completed_at_utc=now())
            record['metadata_snapshot']=metadata_snapshot(destination,record)
        except Exception as exc:
            record.update(state='stopped_with_error',error=str(exc),source_retained=True);report['state']='stopped';save();raise
        save()
    report.update(state='copy_batch_complete_cloud_unverified',completed_at_utc=now());save();return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('probe');p.add_argument('--source',required=True);p.add_argument('--destination',required=True);p.add_argument('--attestation');p.add_argument('--output',required=True)
    p=sub.add_parser('copy-batch');p.add_argument('--plan',required=True);p.add_argument('--report',required=True);p.add_argument('--max-mib',type=int,default=256);p.add_argument('--reserve-gib',type=float,default=8)
    args=parser.parse_args()
    if args.command=='probe':
        result=probe(args.source,args.destination,args.attestation);Path(args.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({k:result[k] for k in ('read_only_drivefs_evidence','independent_server_evidence','free_cache_volume_bytes')},indent=2))
    else:copy_batch(args.plan,args.report,args.max_mib*1024**2,int(args.reserve_gib*GIB))
