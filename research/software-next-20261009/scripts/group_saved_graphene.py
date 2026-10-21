"""Bounded offline spatial grouping of verified saved graphene observations.

One field is decoded at a time. Compressed observation geometry and an R-tree
live in SQLite; grouping loads at most32 prepared observations. Original raw,
analysis shards and scanner state are read-only. DEDUP_STOP affects this job only.
"""
from pathlib import Path
import os
for _name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[_name]='1'
import argparse
from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
import math
import sqlite3
import sys
import time
import zlib

WORKSPACE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORKSPACE))
from flakefinder.graphene_dedup import DedupOptions, _canonical, _prepare, _pair
from flakefinder.graphene_review import GrapheneReview


def _save(path,value):
    raw=json.dumps(value,sort_keys=True,allow_nan=False).encode();temporary=path.with_suffix(path.suffix+'.partial')
    with temporary.open('wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def _hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def open_database(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.executescript('''PRAGMA cache_size=-8192; PRAGMA temp_store=FILE; PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS fields(field TEXT PRIMARY KEY, sha TEXT NOT NULL, state TEXT NOT NULL, reason TEXT);
      CREATE TABLE IF NOT EXISTS observations(n INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, field TEXT NOT NULL,
        data BLOB NOT NULL, eligible INTEGER NOT NULL, measured INTEGER, clear INTEGER, covariance REAL,
        fraction REAL, area REAL, quality TEXT, reason TEXT, group_id INTEGER);
      CREATE VIRTUAL TABLE IF NOT EXISTS bounds USING rtree(n,minx,maxx,miny,maxy);
      CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY, representative TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS observations_group ON observations(group_id);
      CREATE INDEX IF NOT EXISTS observations_order ON observations(eligible DESC,measured DESC,clear DESC,
        covariance,fraction DESC,area DESC,id);
    ''')
    return db


def store_field(db, field, sha, payload, context, transform, options, minimum_area, check=lambda:None):
    """Transactionally index only minimal grouping evidence, never full shard data."""
    keys=('id','field','run_id','chip_id','coordinate_frame_id','source_tiff_sha256','source_transform_sha256',
        'source_shape_hw','source_position_um','polygon_source_px','holes_source_px','polygon_px','holes_px',
        'contrast_rgb','contrast_covariance','measurement','material_pixel_fraction','area_um2')
    with db:
        for row in payload['rows']:
            check();area=row.get('area_um2')
            if not isinstance(area,(int,float)) or not math.isfinite(area) or area<minimum_area:continue
            slim={k:row[k] for k in keys if k in row}
            # Source coordinates have already been adapted by the verified reader.
            if 'polygon_source_px' in slim:slim.pop('polygon_px',None)
            if 'holes_source_px' in slim:slim.pop('holes_px',None)
            try:item=_prepare(slim,context,transform,options);quality=item['quality'];reason=None
            except (ValueError,TypeError,KeyError,IndexError) as exc:item=None;quality={};reason=str(exc)
            blob=zlib.compress(json.dumps(slim,separators=(',',':'),allow_nan=False).encode(),3)
            cursor=db.execute('''INSERT INTO observations(id,field,data,eligible,measured,clear,covariance,fraction,area,quality,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(row['id'],field,blob,int(item is not None),
                int(quality.get('has_measured_interior',False)),int(quality.get('clear_of_image_border',False)),
                quality.get('contrast_covariance_trace',0.),quality.get('material_pixel_fraction',0.),float(area),
                json.dumps(quality),reason))
            if item is not None:
                low,high=item['bounds']
                db.execute('INSERT INTO bounds VALUES(?,?,?,?,?)',(cursor.lastrowid,float(low[0]),float(high[0]),float(low[1]),float(high[1])))
        db.execute('INSERT INTO fields VALUES(?,?,?,?)',(field,sha,payload.get('assessment_state') or 'assessed',payload.get('reason')))


def group_database(db,context,transform,options,check=lambda:None,progress=lambda **kw:None):
    cache=OrderedDict();pair_checks=0
    def load(n):
        if n in cache:cache.move_to_end(n);return cache[n]
        saved=db.execute('SELECT data FROM observations WHERE n=?',(n,)).fetchone()
        row=json.loads(zlib.decompress(saved['data']));item=_prepare(row,context,transform,options)
        cache[n]=item
        while len(cache)>32:cache.popitem(last=False)
        return item
    pending=db.execute('''SELECT n,id,eligible FROM observations WHERE group_id IS NULL
        ORDER BY eligible DESC,measured DESC,clear DESC,covariance,fraction DESC,area DESC,id''')
    # Streaming cursor and disk-backed grouping avoid an all-observation geometry list.
    finished=db.execute('SELECT COUNT(*) FROM observations WHERE group_id IS NOT NULL').fetchone()[0]
    for row in pending:
        check();group_id=None
        if row['eligible']:
            item=load(row['n']);low,high=item['bounds']
            candidates=db.execute('''SELECT DISTINCT o.group_id FROM bounds b JOIN observations o ON b.n=o.n
                WHERE o.group_id IS NOT NULL AND b.maxx>=? AND b.minx<=? AND b.maxy>=? AND b.miny<=?
                ORDER BY o.group_id''',(float(low[0]),float(high[0]),float(low[1]),float(high[1])))
            best=-1.
            for candidate in candidates:
                score=1.;accepted=True
                for member in db.execute('SELECT n FROM observations WHERE group_id=? ORDER BY id',(candidate[0],)):
                    check();evidence=_pair(item,load(member['n']),options);pair_checks+=1
                    if not evidence['accepted']:accepted=False;break
                    score=min(score,evidence['iou'])
                if accepted and score>best:best=score;group_id=candidate[0]
        if group_id is None:group_id=db.execute('INSERT INTO groups(representative) VALUES(?)',(row['id'],)).lastrowid
        db.execute('UPDATE observations SET group_id=? WHERE n=?',(group_id,row['n']))
        finished+=1
        if finished%100==0:db.commit()
        progress(phase='grouping',grouped_observations=finished,pair_checks_this_invocation=pair_checks)
    db.commit()
    return {'pair_checks_this_invocation':pair_checks,'prepared_cache_capacity':32}


def export_groups(db,context,transform,options,check=lambda:None):
    groups=[]
    for group in db.execute('''SELECT g.id,g.representative,COUNT(*) count FROM groups g JOIN observations o ON g.id=o.group_id
        GROUP BY g.id HAVING COUNT(*)>1 ORDER BY g.id'''):
        check();members=list(db.execute('SELECT id,data,quality FROM observations WHERE group_id=? ORDER BY id',(group['id'],)))
        ids=[r['id'] for r in members];pair_evidence=[]
        prepared=[_prepare(json.loads(zlib.decompress(r['data'])),context,transform,options) for r in members]
        for n,a in enumerate(prepared):
            for b in prepared[n+1:]:
                check();evidence=_pair(a,b,options)
                if not evidence['accepted']:raise ValueError('Saved group violates complete-link membership')
                pair_evidence.append({'observation_ids':[a['row']['id'],b['row']['id']],**evidence})
        groups.append({'id':'graphene-group-'+_canonical({'context':context,'observations':ids})[:20],
            'representative_id':group['representative'],'observation_ids':ids,'observation_count':len(ids),
            'status':'possible_same_object','pair_evidence':pair_evidence,
            'representative_quality':json.loads(next(r['quality'] for r in members if r['id']==group['representative']))})
    return groups


def run(args):
    import cv2
    cv2.setNumThreads(1)
    from analyze_graphene_run import cpu_policy, memory_snapshot
    cpu=cpu_policy()
    project=Path(args.project).resolve();reader=GrapheneReview(project)
    run_folder,request,request_hash=reader._run(args.run_id)
    offline=reader._offline(run_folder,args.run_id,request_hash,args.chip)
    if offline is None:raise ValueError('Select one pinned offline analysis before grouping')
    source,_,commits,stale,selection=offline
    if stale:raise ValueError('Offline analysis metadata is temporarily unreadable')
    if selection['chip']!=args.chip:raise ValueError('Selected analysis chip differs from requested chip')
    if not math.isfinite(args.minimum_area) or args.minimum_area<0:raise ValueError('Finite nonnegative area required')
    import re
    if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,63}',args.version):raise ValueError('Simple derived review version required')
    output=project/'analysis/graphene'/args.run_id/args.chip/args.version
    if output.resolve()!=output or output==source:raise ValueError('Separate nonredirected derived output required')
    output.mkdir(parents=True,exist_ok=True)
    # An independent owner lock prevents two derivations changing one database.
    lock=(output/'dedup-owner.lock').open('a+b');lock.write(b'0');lock.flush();lock.seek(0)
    if os.name=='nt':
        import msvcrt
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:lock.close();raise RuntimeError('This grouping output already has an owner')
    state={'state':'starting','phase':'indexing','no_acquisition_performed':True,'pid':os.getpid(),'started_at':time.time(),
        'cpu':cpu,'memory_at_start':memory_snapshot()}
    last_progress=0.;db=None
    def progress(force=False,**update):
        nonlocal last_progress
        state.update(update);now=time.monotonic()
        if force or now-last_progress>=1:
            state['updated_at']=time.time();state['memory']=memory_snapshot()
            _save(output/'dedup-progress.json',state);last_progress=now
    def check():
        if (output/'DEDUP_STOP').exists():raise InterruptedError('Independent offline grouping STOP requested')
    try:
        check();options=DedupOptions()
        manifest_path=run_folder/args.chip/'manifest.json';batch_path=run_folder/'batch.json'
        manifest_bytes=manifest_path.read_bytes();batch_bytes=batch_path.read_bytes()
        manifest=json.loads(manifest_bytes);batch=json.loads(batch_bytes)
        transform=manifest['transform'];frame=batch['coordinate_frame_id'];del manifest,batch
        context={'run_id':args.run_id,'chip_id':args.chip,'coordinate_frame_id':frame}
        plan=json.loads((source/'plan.json').read_bytes())
        pin={'schema':1,'run_id':args.run_id,'chip_id':args.chip,'plan_sha256':selection['plan_sha256'],
            'ledger_sha256':_hash(source/'ledger.json'),'manifest_sha256':plan['pin']['manifest_sha256'],
            'batch_sha256':hashlib.sha256(batch_bytes).hexdigest(),'minimum_area_um2':args.minimum_area,'context':context,
            'source_analysis_relative_path':source.relative_to(project).as_posix(),'options':asdict(options),
            'source_sha256':{str(Path(__file__).resolve()):_hash(Path(__file__).resolve()),
                'graphene_dedup.py':_hash(WORKSPACE/'flakefinder/graphene_dedup.py')}}
        if hashlib.sha256(manifest_bytes).hexdigest()!=pin['manifest_sha256']:
            raise ValueError('Source manifest changed before geometry preparation')
        del manifest_bytes,batch_bytes
        pin_path=output/'dedup-pin.json'
        if pin_path.exists():
            if json.loads(pin_path.read_bytes())!=pin:raise ValueError('Grouping inputs/config/code changed; use a new version')
        else:_save(pin_path,pin)
        db=open_database(output/'dedup.sqlite');progress(force=True,state='running')
        done={r['field'] for r in db.execute('SELECT field FROM fields')}
        unavailable={};added_fields=0
        for field,commit in sorted(commits.items()):
            check();expected=commit['hashes'][commit['field_json']]
            if field not in done:
                try:
                    payload=reader._shard(source,field,commit)
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    unavailable[field]=str(exc);continue
                store_field(db,field,expected,payload,context,transform,options,args.minimum_area,check)
                del payload;reader._shards.clear();done.add(field);added_fields+=1
            progress(phase='indexing',indexed_fields=len(done),selected_fields=len(commits),unavailable_fields=len(unavailable))
        def verify_pins():
            for path,expected in ((source/'plan.json',pin['plan_sha256']),(source/'ledger.json',pin['ledger_sha256']),
                (manifest_path,pin['manifest_sha256']),(batch_path,pin['batch_sha256']),(run_folder/'scan-request.json',request_hash)):
                check()
                if _hash(path)!=expected:raise ValueError('Pinned grouping source changed: '+str(path))
        verify_pins()
        if added_fields and db.execute('SELECT COUNT(*) FROM groups').fetchone()[0]:
            # Newly readable evidence can change global representative priority.
            # Rebuild only derived memberships; indexed source evidence is reused.
            with db:
                db.execute('UPDATE observations SET group_id=NULL');db.execute('DELETE FROM groups')
        statistics=group_database(db,context,transform,options,check,progress)
        groups=export_groups(db,context,transform,options,check);verify_pins()
        count=db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        group_count=db.execute('SELECT COUNT(*) FROM groups').fetchone()[0]
        artifact={**pin,'groups':groups,'source_fields':{r['field']:r['sha'] for r in db.execute('SELECT field,sha FROM fields')},
            'unavailable_fields':unavailable,'observation_count':count,'total_group_count':group_count,
            'singleton_count':group_count-len(groups),'merged_observation_count':count-group_count,**statistics,
            'ineligible_reasons':{r['reason']:r['n'] for r in db.execute('SELECT reason,COUNT(*) n FROM observations WHERE eligible=0 GROUP BY reason')},
            'groups_scope':'Only multi-observation groups are listed. All unlisted observations remain individual source observations.',
            'qualification':'Reversible cross-tile grouping of saved optical observations at or above the stated area; '
                'unknown optical classes remain eligible. No source masks are changed, no object identities or layers confirmed.'}
        _save(output/'dedup.json',artifact)
        progress(force=True,state='complete',phase='complete',observation_count=count,total_group_count=group_count,
            multi_observation_groups=len(groups),merged_observation_count=count-group_count,
            unavailable_fields=len(unavailable),output=str(output/'dedup.json'))
        return state
    except InterruptedError as exc:progress(force=True,state='stopped',reason=str(exc));return state
    except BaseException as exc:progress(force=True,state='failed',reason=str(exc));raise
    finally:
        if db is not None:db.close()
        lock.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True);parser.add_argument('--run-id',required=True)
    parser.add_argument('--chip',default='chip001');parser.add_argument('--version',default='review-v2')
    parser.add_argument('--minimum-area',type=float,default=25.)
    print(json.dumps(run(parser.parse_args()),sort_keys=True))
