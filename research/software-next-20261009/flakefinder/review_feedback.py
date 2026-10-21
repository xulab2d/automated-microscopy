"""Durable human feedback bound to exact saved observation provenance.

One atomic per-run state contains both the append-only logical audit and its
current projection. There is no separate event/index publication crash window.
Geometry lives only in audit events; annotations never alter scientific masks.
The shared RLock serializes server threads, including separate store instances.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
import uuid


LABELS=frozenset({'thin_hbn','encapsulation_hbn','bulk','not_flake','good_flake'})
_LOCK=threading.RLock()
_NAME=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
_HASH=re.compile(r'[a-f0-9]{64}\Z')


class FeedbackConflict(ValueError):
    """A stale revision or a conflicting reuse of an idempotency key."""


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def identity_digest(identity):
    """Stable digest; validation of current source authority remains upstream."""
    return hashlib.sha256(_canonical(identity).encode('utf-8')).hexdigest()


def _run(value):
    if not isinstance(value,str) or not _NAME.fullmatch(value) or value in ('.','..'):
        raise ValueError('Invalid review run ID')
    return value


def _identity(identity):
    if not isinstance(identity,dict):raise ValueError('Review source identity must be an object')
    required={'run_id','observation','analysis_relative_path','plan_sha256','field_sha256',
        'source_tiff_sha256','scan_request_sha256','material'}
    if not required.issubset(identity):raise ValueError('Incomplete review source identity')
    run=_run(identity['run_id']);observation=identity['observation']
    if not isinstance(observation,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*/[1-9][0-9]*',observation):
        raise ValueError('Invalid review observation ID')
    relative=identity['analysis_relative_path']
    if not isinstance(relative,str) or not relative or '\\' in relative or ':' in relative or '\x00' in relative:
        raise ValueError('Analysis path must be a project-relative POSIX path')
    path=PurePosixPath(relative)
    standard=path.parts[0]=='analysis' and run in path.parts
    incremental=path.parts in (('data',run,'analysis-graphene-incremental'),('data',run,'analysis-hbn-incremental'))
    if path.is_absolute() or any(part in ('','..','.') for part in relative.split('/')) or not (standard or incremental):
        raise ValueError('Analysis path must remain inside this run project analysis')
    if identity['material'] not in ('hBN','graphene'):raise ValueError('Unsupported review material')
    for key in ('plan_sha256','field_sha256','source_tiff_sha256','scan_request_sha256'):
        value=identity[key]
        if value is None and key in ('plan_sha256','source_tiff_sha256'):continue
        if not isinstance(value,str) or not _HASH.fullmatch(value):raise ValueError('Invalid review source hash: '+key)
    # JSON round-trip rejects nonfinite/nonserializable evidence and detaches it
    # from mutable request objects before it participates in an event identity.
    return json.loads(_canonical(identity))


class ReviewFeedback:
    identity_digest=staticmethod(identity_digest)

    def __init__(self,root):
        self.root=Path(root).resolve()

    def _path(self,run_id):
        run_id=_run(run_id)
        base=self.root/'annotations'/'flake-review'
        path=base/run_id/'current.json'
        if not path.resolve().is_relative_to(self.root):raise ValueError('Review annotation path escapes project')
        return path

    def _load(self,run_id):
        path=self._path(run_id)
        if not path.exists():return {'schema':1,'run_id':run_id,'events':[],'current':{}},{}
        state=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state,dict) or state.get('schema')!=1 or state.get('run_id')!=run_id or not isinstance(state.get('events'),list) or not isinstance(state.get('current'),dict):
            raise ValueError('Invalid saved review feedback state; existing evidence retained')
        rebuilt={};clients={}
        for event in state['events']:
            if not isinstance(event,dict):raise ValueError('Invalid saved review audit event')
            record=event.get('record');client=event.get('client_event_id')
            if not isinstance(record,dict) or not isinstance(client,str) or client in clients:
                raise ValueError('Invalid or duplicate saved review audit event')
            identity=_identity(record.get('identity'));key=identity_digest(identity)
            prior=rebuilt.get(key,{}).get('revision',0)
            if identity['run_id']!=run_id or event.get('identity_digest')!=key or record.get('revision')!=prior+1:
                raise ValueError('Saved review identity/revision mismatch; existing evidence retained')
            if record.get('label') is not None and record.get('label') not in LABELS:raise ValueError('Invalid saved review label')
            rebuilt[key]=record;clients[client]=event
        if rebuilt!=state['current']:raise ValueError('Saved review current projection differs from audit; existing evidence retained')
        return state,clients

    def snapshot(self,run_id):
        with _LOCK:
            state,_=self._load(_run(run_id))
            return deepcopy(state['current'])

    @staticmethod
    def _response(event,*,idempotent=False):
        record=event['record']
        return {'revision':record['revision'],'annotation':deepcopy(record) if record['label'] is not None else None,
            'identity_digest':event['identity_digest'],'event_id':event['event_id'],
            'updated_at':record['updated_at'],'idempotent':idempotent}

    def append(self,identity,*,label,actor,expected_revision,client_event_id,source_region):
        identity=_identity(identity)
        if label is not None and (not isinstance(label,str) or label not in LABELS):raise ValueError('Unsupported human review label')
        if not isinstance(actor,str) or not actor.strip() or len(actor)>200 or any(ord(c)<32 for c in actor):raise ValueError('A bounded reviewer name is required')
        if type(expected_revision) is not int or expected_revision<0:raise ValueError('Nonnegative integer expected revision required')
        if not isinstance(client_event_id,str) or not client_event_id or len(client_event_id)>200 or any(ord(c)<32 for c in client_event_id):raise ValueError('A bounded client event ID is required')
        if not isinstance(source_region,dict):raise ValueError('Source region geometry/provenance must be an object')
        source_region=json.loads(_canonical(source_region))
        request={'identity':identity,'label':label,'actor':actor,'expected_revision':expected_revision,'source_region':source_region}
        request_hash=hashlib.sha256(_canonical(request).encode('utf-8')).hexdigest()
        run_id=identity['run_id'];key=identity_digest(identity)
        with _LOCK:
            state,clients=self._load(run_id)
            if client_event_id in clients:
                event=clients[client_event_id]
                if event.get('request_sha256')!=request_hash:raise FeedbackConflict('Client event ID was already used for a different review request')
                return self._response(event,idempotent=True)
            previous=state['current'].get(key);revision=previous['revision'] if previous else 0
            if revision!=expected_revision:raise FeedbackConflict(f'Review revision changed: expected {expected_revision}, current {revision}')
            stamp=datetime.now(timezone.utc).isoformat()
            record={'identity':identity,'label':label,'revision':revision+1,'actor':actor,'updated_at':stamp}
            event={'event_id':uuid.uuid4().hex,'client_event_id':client_event_id,'request_sha256':request_hash,
                'identity_digest':key,'record':record,'source_region':source_region,
                'operation':'clear' if label is None else 'label','previous_revision':revision}
            state['events'].append(event);state['current'][key]=record
            path=self._path(run_id);path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name('current.'+uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('x',encoding='utf-8',newline='\n') as stream:
                    stream.write(_canonical(state));stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,path)
            finally:
                # An unpublished temporary is never treated as committed. An
                # interrupted process may leave one; a reload ignores it.
                if temporary.exists():temporary.unlink()
            return self._response(event)
