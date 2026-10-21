"""Read-only, run-pinned projection of committed graphene analysis shards.

The watcher's small top-ranked catalog is not an observation index. This reader
indexes every committed field, validates field JSON against its ledger digest,
and only returns one page of full geometries at a time. No instrument imports.
"""
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote
import hashlib
import json
import math
import re
import threading


class GrapheneReview:
    def __init__(self, project):
        self.root = Path(project).resolve()
        self._index = {}
        self._shards = OrderedDict()
        self._snapshots = {}
        self._lock = threading.RLock()

    @staticmethod
    def _inside(root, relative):
        value = Path(relative)
        if value.is_absolute() or value.drive or '..' in value.parts:
            raise ValueError('Analysis path must stay inside this run')
        path = (root/value).resolve()
        if not path.is_relative_to(root): raise ValueError('Analysis path leaves this run')
        return path

    def _read(self, path, default=None, limit=32*1024**2):
        try:
            stat = path.stat(); signature=(stat.st_mtime_ns,stat.st_size)
            previous=self._snapshots.get(str(path))
            if previous and previous[0]==signature: return previous[1],False
            if stat.st_size > limit: raise ValueError('Saved analysis record is too large')
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(value,dict): raise ValueError('Expected a saved analysis object')
            if path.name=='manifest.json':
                completed=value.get('completed',{})
                value={'completed':dict.fromkeys(completed,{}) if isinstance(completed,dict) else {}}
            self._snapshots[str(path)] = (signature,value)
            return value, False
        except (OSError, ValueError, UnicodeError):
            if str(path) in self._snapshots: return self._snapshots[str(path)][1], True
            return default, path.exists()

    def _run(self, run_id):
        if not isinstance(run_id,str) or not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',run_id):
            raise ValueError('Choose one exact scan run')
        folder = self._inside(self.root,'data/'+run_id)
        path = folder/'scan-request.json'
        raw = path.read_bytes()
        request = json.loads(raw)
        if request.get('id') != run_id or request.get('settings',{}).get('material') != 'graphene':
            raise ValueError('This page only reviews the selected graphene scan')
        return folder, request, hashlib.sha256(raw).hexdigest()

    def _shard(self, output, field, commit):
        relative = commit.get('field_json')
        if not isinstance(relative,str): raise ValueError('Missing committed field path')
        expected = commit.get('hashes',{}).get(relative)
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
            raise ValueError('Missing committed field checksum')
        path = self._inside(output,relative)
        stat = path.stat(); key = (str(path),expected,stat.st_mtime_ns,stat.st_size)
        if key in self._shards:
            self._shards.move_to_end(key)
            return self._shards[key]
        if stat.st_size > 32*1024**2: raise ValueError('Field record too large')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected: raise ValueError('Committed field checksum mismatch')
        value = json.loads(raw)
        if value.get('field') != field or not isinstance(value.get('rows'),list):
            raise ValueError('Committed field identity differs from ledger')
        for row in value['rows']:
            if not isinstance(row,dict) or row.get('field') != field or not str(row.get('id','')).startswith(field+'/'):
                raise ValueError('Region identity differs from source field')
        self._shards[key] = value
        while len(self._shards)>12: self._shards.popitem(last=False)
        return value

    @staticmethod
    def _filtered(row):
        return bool(row.get('filter_reasons')) or any(s.get('status') in ('fail','rejected','below_priority') for s in row.get('sieve_results',[]))

    def data(self, run_id, *, category='shortlist', chip='', field='', offset=0, limit=48, minimum_area=25.):
        if category not in ('shortlist','uncertain','rejected','all'): raise ValueError('Unknown proposal view')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=96:
            raise ValueError('Invalid page bounds')
        if not isinstance(minimum_area,(int,float)) or not math.isfinite(minimum_area) or minimum_area<0:
            raise ValueError('Minimum area must be nonnegative')
        if chip and not re.fullmatch(r'[A-Za-z0-9_-]+',chip): raise ValueError('Invalid chip key')
        if field and not re.fullmatch(r'[A-Za-z0-9_-]+/tile_\d+',field): raise ValueError('Invalid field key')
        with self._lock:
            run,request,request_hash = self._run(run_id)
            output = self._inside(run,'analysis-graphene-incremental')
            status,stale = self._read(output/'status.json',{})
            acquisition,_ = self._read(run/'scan-status.json',{})
            ledger,ledger_stale = self._read(output/'ledger.json',{})
            if status and (status.get('run_id') != run_id or status.get('scan_request_sha256') != request_hash):
                raise ValueError('Analysis status is not pinned to this scan request')
            rows=[];fields=[];warnings=[];commits={}
            for key,commit in ledger.items():
                if not re.fullmatch(r'[A-Za-z0-9_-]+/tile_\d+',key) or not isinstance(commit,dict):
                    warnings.append('Invalid ledger entry omitted');continue
                digest=commit.get('hashes',{}).get(commit.get('field_json'))
                index_key=(run_id,key,digest)
                try:
                    if index_key not in self._index:
                        payload=self._shard(output,key,commit)
                        compact=[{k:row.get(k) for k in ('id','field','disposition','area_um2','rank','filter_reasons','sieve_results')} for row in payload['rows']]
                        state=payload.get('assessment_state') or 'assessed'
                        self._index[index_key]=(compact,dict(field=key,state=state,reason=payload.get('reason'),region_count=len(compact)))
                    compact,summary=self._index[index_key]
                    rows.extend(compact);fields.append(summary);commits[key]=commit
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    fields.append(dict(field=key,state='record_unavailable',reason=str(exc),region_count=None))
                    warnings.append(key+': '+str(exc))
            # Add every manifest-committed source field, including fields that
            # have not yet reached the analysis worker or were unassessable.
            known={item['field'] for item in fields}
            for manifest_path in sorted(run.glob('chip*/manifest.json')):
                if not manifest_path.resolve().is_relative_to(run): continue
                manifest,_=self._read(manifest_path,{})
                completed=manifest.get('completed',{})
                if not isinstance(completed,dict): continue
                for tile in completed:
                    if not re.fullmatch(r'tile_\d+',tile): continue
                    key=manifest_path.parent.name+'/'+tile
                    if key not in known: fields.append(dict(field=key,state='pending',reason=None,region_count=None))
            chips=sorted({v['field'].split('/')[0] for v in fields})
            matched=[r for r in rows if (not chip or r['field'].split('/')[0]==chip) and (not field or r['field']==field) and isinstance(r.get('area_um2'),(int,float)) and r['area_um2']>=minimum_area]
            def matches(row,kind):
                return kind=='all' or kind=='rejected' and self._filtered(row) or kind in ('shortlist','uncertain') and row.get('disposition')==kind
            counts={kind:sum(matches(r,kind) for r in matched) for kind in ('shortlist','uncertain','rejected','all')}
            selected=sorted((r for r in matched if matches(r,category)),key=lambda r:(-float(r.get('rank') or 0),r['field'],r['id']))
            count=len(selected);offset=min(offset,max(0,((count-1)//limit)*limit))
            result_rows=[]
            for item in selected[offset:offset+limit]:
                try:
                    payload=self._shard(output,item['field'],commits[item['field']])
                    row=next(r for r in payload['rows'] if r['id']==item['id'])
                    preview=self._inside(run,row['preview'])
                    if preview.suffix.lower() not in ('.jpg','.jpeg','.png','.webp'): raise ValueError('Invalid preview type')
                    result_rows.append({**row,'preview_url':'/files/'+quote(preview.relative_to(self.root).as_posix(),safe='/'),
                        'source_field_reference':commits[item['field']]['field_json']})
                except (OSError,ValueError,TypeError,KeyError,StopIteration) as exc:
                    warnings.append(item['field']+': '+str(exc))
            source=None
            if field:
                preview=self._inside(run,field.split('/')[0]+'/tiles/'+field.split('/')[1]+'.jpg')
                source=dict(field=field,preview_url='/files/'+quote(preview.relative_to(self.root).as_posix(),safe='/'),
                    **next(({k:v for k,v in f.items() if k!='field'} for f in fields if f['field']==field),{'state':'not_committed'}))
            return dict(run_id=run_id,sample_id=request['settings'].get('sample_id'),settings=request['settings'],
                acquisition=acquisition,status=status or {'state':'not_started','processed_fields':0,'available_fields':len(fields)},
                stale=bool(stale or ledger_stale),rows=result_rows,filtered_count=count,counts=counts,
                offset=offset,limit=limit,category=category,minimum_area_um2=minimum_area,
                chip_ids=chips,fields=sorted(fields,key=lambda v:v['field']),source=source,
                indexed_fields=len(commits),source_region_count=len(rows),warnings=warnings[:20],
                qualification='Saved assessed regions only. Filtered-out regions retain their original uncertain disposition. No material identity or layer assignment is confirmed.')
