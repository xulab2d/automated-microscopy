"""Explicit camera-export release after durable scientific observation receipts.

No hardware clients, timers, age pruning, or final/scientific image removal.
The methods are installed on the ZEN adapter; other adapters remain unaffected.
"""
from dataclasses import asdict
from contextvars import ContextVar
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid

import cv2
import numpy as np

from .model import AcquisitionError, SafetyError, atomic_json
from .imaging import preview

PENDING_JOBS=4
PENDING_BYTES=512*1024**2
NEXT_JOB_BYTES=128*1024**2
_review_hashes=ContextVar('vendor_review_hashes',default=None)

def file_hash(path):
    cache=_review_hashes.get()
    if cache is not None:return cache.digest(path,_file_hash)
    return _file_hash(path)

def _file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def plain(value):return json.loads(json.dumps(value,sort_keys=True))
def redirected(path):
    info=Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)

def durable(path):
    with Path(path).open('rb+') as stream:os.fsync(stream.fileno())

def pending_frame(self,frame):
    if self.keep_vendor_results:return False
    p=frame.metadata.get('zen_vendor_result',{})
    record=self._vendor_jobs.get(p.get('capture_id'))
    return bool(record and p.get('adapter_session_id')==self._vendor_session_id and 'receipt' not in record)

def check_pending(self):
    """Bound unacknowledged output; retain and stop on failure, never prune it."""
    if getattr(self,'keep_vendor_results',True):return
    root=self._vendor_capture_root
    from .vendor_backlog import validated_reviews
    # Retain all semantic validation each time. Only SHA reads are reused while
    # Windows deny-write/delete handles preserve the exact verified bytes.
    if not hasattr(self,'_vendor_review_hashes'):
        from .locked_hashes import LockedHashes
        self._vendor_review_hashes=LockedHashes() if os.name=='nt' else None
    cache=self._vendor_review_hashes
    token=_review_hashes.set(cache)
    try:reviewed=validated_reviews(root)
    finally:_review_hashes.reset(token)
    # Snapshot unknown prior jobs, but independently revalidate every review
    # before each capture. Reviewed originals remain present and hash-pinned.
    if not hasattr(self,'_vendor_prior_pending'):
        prior=[]
        if root.exists():
            for child in root.iterdir():
                if child.is_dir() and not redirected(child) and child.name not in self._vendor_jobs:
                    for p in child.glob('*.czi'):
                        prior.append((child.name,p.stat().st_size))
        self._vendor_prior_pending=prior
    count=sum(identifier not in reviewed for identifier,_ in self._vendor_prior_pending)
    size=sum(n for identifier,n in self._vendor_prior_pending if identifier not in reviewed)
    for record in self._vendor_jobs.values():
        if 'receipt' in record:continue
        count+=1
        folder=Path(record['folder'])
        if folder.exists():size+=sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
    if count>=PENDING_JOBS or size+NEXT_JOB_BYTES>PENDING_BYTES:
        raise AcquisitionError(f'Unacknowledged vendor output limit: {count} jobs, {size} bytes. Persist/review pending work before another capture; no files were deleted.')
    return {'pending_jobs':count,'pending_bytes':size,'maximum_jobs':PENDING_JOBS,'maximum_bytes':PENDING_BYTES,
            'reviewed_retained_jobs':len(reviewed)}

def checkpoint_observation(scope,frame,folder,kind,measurement,*,selected_raw=None):
    """Persist score/preview/metadata before release; return retained for no output.

    A focus crop is evidence about its parent capture, never a duplicate raw
    full frame. FocusROIAdapter supplies that exact in-memory parent identity.
    """
    if folder is None or not hasattr(scope,'vendor_frame_pending') or not scope.vendor_frame_pending(frame):return None
    from .storage import managed_output_path
    requested_folder=managed_output_path(folder)
    # All observations stay inside their run, without repeating the focus
    # hierarchy in each UUID filename. This also keeps later raster AF below
    # the Windows path limit; capture IDs remain unique across focus anchors.
    run_root=next((p for p in (requested_folder,*requested_folder.parents)
                   if p.name.startswith('scan-') and p.parent.name=='data'),None)
    folder=(run_root or requested_folder)/'observations';folder.mkdir(parents=True,exist_ok=True)
    full=scope.retention_full_frame(frame) if hasattr(scope,'retention_full_frame') else frame
    provenance=full.metadata['zen_vendor_result'];identifier=provenance['capture_id']
    path=folder/(identifier+'.json');jpg=folder/(identifier+'.jpg')
    image=preview(frame.rgb,frame.metadata.get('white_level'))
    h,w=image.shape[:2]
    if max(h,w)>640:image=cv2.resize(image,(max(1,round(w*640/max(h,w))),max(1,round(h*640/max(h,w)))),interpolation=cv2.INTER_AREA)
    temporary=jpg.with_suffix('.tmp.jpg')
    if not cv2.imwrite(str(temporary),image[:,:,::-1],[cv2.IMWRITE_JPEG_QUALITY,85]):raise OSError('Could not persist capture observation preview.')
    temporary.replace(jpg);durable(jpg)
    retained=[]
    if selected_raw is not None:
        raw=Path(selected_raw)
        from .imaging import load_frame
        saved=load_frame(raw)
        if (saved.rgb.shape!=frame.rgb.shape or saved.rgb.dtype!=frame.rgb.dtype or not np.array_equal(saved.rgb,frame.rgb)
                or plain(asdict(saved.position))!=plain(asdict(frame.position))
                or plain(saved.metadata)!=plain(frame.metadata) or saved.sequence!=frame.sequence
                or saved.acquired_at!=frame.acquired_at or not np.array_equal(saved.pixel_size_um,frame.pixel_size_um)):
            raise SafetyError('Selected raw checkpoint differs from the measured observation.')
        for p in (raw,raw.with_suffix('.json')):
            durable(p);retained.append({'path':str(p.resolve()),'bytes':p.stat().st_size,'sha256':file_hash(p)})
    evidence={'schema':1,'kind':'persisted_vendor_observation','observation_kind':str(kind),
        'observation_context_folder':str(requested_folder),
        'capture':plain(provenance),'position':asdict(full.position),'pixel_size_um':list(full.pixel_size_um),
        'sequence':full.sequence,'acquired_at':full.acquired_at,'metadata':plain(full.metadata),
        'measurement':plain(measurement),'analysis_frame':{'position':asdict(frame.position),
            'shape':list(frame.rgb.shape),'dtype':str(frame.rgb.dtype),'metadata':plain(frame.metadata),
            'raw_rgb_sha256':hashlib.sha256(np.ascontiguousarray(frame.rgb).tobytes()).hexdigest()},
        'preview':{'path':str(jpg.resolve()),'bytes':jpg.stat().st_size,'sha256':file_hash(jpg)},
        'selected_raw_preserved':retained,'created_at':time.time(),
        'retention_statement':'Unselected vendor export is retired after this observation; this receipt is not a full-frame raw duplicate.'}
    atomic_json(path,evidence);durable(path)
    receipt=scope.ack_observed_frame(full,path)
    if hasattr(scope,'forget_retention_frame'):scope.forget_retention_frame(frame)
    return receipt

def acknowledge_raw(scope,frame,path):
    """Legacy scan/revisit final frames use the original full-raw verifier."""
    if hasattr(scope,'vendor_frame_pending') and scope.vendor_frame_pending(frame):
        return scope.ack_persisted_frame(frame,path)
    return None

def ack_observed(self,frame,evidence_path):
    if self.keep_vendor_results:return {'status':'retained','reason':'keep_vendor_results is enabled'}
    from .storage import resolve_output_path
    with self._vendor_storage_lock:
        p=frame.metadata.get('zen_vendor_result',{});identifier=p.get('capture_id');record=self._vendor_jobs.get(identifier)
        if record is None or p.get('adapter_session_id')!=self._vendor_session_id:raise SafetyError('Observation is not from this adapter session.')
        if not record['completed'] or not record['unloaded'] or 'original_metadata' not in record:raise SafetyError('Observation job has not completed and unloaded.')
        if plain(p)!=record['provenance']:raise SafetyError('Observation capture provenance changed.')
        if (list(frame.rgb.shape)!=p['raw_shape'] or str(frame.rgb.dtype)!=p['raw_dtype'] or
            hashlib.sha256(np.ascontiguousarray(frame.rgb).tobytes()).hexdigest()!=p['raw_rgb_sha256']):
            raise SafetyError('Observation parent must be the original captured full frame.')
        root=self._vendor_capture_root
        if (root!=resolve_output_path(self._vendor_logical_root)
                or self.job_output_dir!=root or root.absolute()!=root.resolve() or redirected(root)):
            raise SafetyError('Vendor observation cleanup root changed or is redirected.')
        folder=Path(record['folder'])
        if folder.parent!=root or folder.name!=identifier or uuid.UUID(hex=identifier).hex!=identifier:raise SafetyError('Observation target is not the owned UUID directory.')
        path=Path(evidence_path).resolve()
        if path.is_relative_to(root) or redirected(path) or path.stat().st_size>2*1024**2:raise SafetyError('Invalid durable observation destination.')
        durable(path);evidence=json.loads(path.read_text(encoding='utf-8'))
        if evidence.get('kind')!='persisted_vendor_observation' or evidence.get('capture')!=p:raise SafetyError('Observation receipt does not match the capture.')
        for key in ('position','pixel_size_um','sequence','acquired_at'):
            if evidence.get(key)!=plain(record[key]):raise SafetyError('Observation acquisition metadata changed: '+key)
        for key,original in record['original_metadata'].items():
            if evidence.get('metadata',{}).get(key)!=original:raise SafetyError('Observation original metadata changed: '+key)
        references=[evidence.get('preview',{})]+evidence.get('selected_raw_preserved',[])
        for ref in references:
            target=Path(ref['path']).resolve()
            if target.is_relative_to(root) or redirected(target):raise SafetyError('Observation reference is redirected or inside disposable vendor output.')
            durable(target)
            if target.stat().st_size!=ref['bytes'] or file_hash(target)!=ref['sha256']:raise SafetyError('Persisted observation reference changed.')
        if 'receipt' in record:return dict(record['receipt'])
        if not folder.is_dir() or redirected(folder) or folder.resolve()!=folder:raise SafetyError('Owned observation folder is absent or redirected.')
        inventory=[];archives={};archive_bytes=0;previews=[];preview_bytes=0
        for child in sorted(folder.iterdir()):
            if redirected(child) or not child.is_file() or child.suffix.lower() not in ('.czi','.czjob','.png','.metadata'):
                raise SafetyError('Unexpected vendor entry; retaining observation output.')
            s=child.stat();inventory.append({'name':child.name,'bytes':s.st_size,'mtime_ns':s.st_mtime_ns,'sha256':file_hash(child)})
            if child.suffix.lower() in ('.czjob','.metadata'):
                archive_bytes+=s.st_size
                if archive_bytes>4*1024**2:raise SafetyError('Vendor metadata archive exceeds bound.')
                archives[child.name]=base64.b64encode(child.read_bytes()).decode('ascii')
            elif child.suffix.lower()=='.png':
                preview_bytes+=s.st_size
                if preview_bytes>16*1024**2:raise SafetyError('Vendor preview archive exceeds bound; retaining output.')
                # Focus observations can be several levels below a long Drive
                # path. Keep vendor previews beside the capture store so Win32
                # CopyFile never receives that nested, >260-character path.
                preview_root=root.parent/'observation-previews'
                preview_root.mkdir(exist_ok=True)
                if redirected(preview_root) or preview_root.resolve().parent!=root.parent:
                    raise SafetyError('Vendor preview archive root is redirected.')
                destination=preview_root/identifier/child.name
                destination.parent.mkdir(exist_ok=True)
                if redirected(destination.parent):raise SafetyError('Vendor preview archive folder is redirected.')
                if destination.exists():
                    if file_hash(destination)!=inventory[-1]['sha256']:raise SafetyError('Unrelated saved vendor preview exists.')
                else:shutil.copyfile(child,destination)
                durable(destination)
                if file_hash(destination)!=inventory[-1]['sha256']:raise SafetyError('Vendor preview archive verification failed.')
                previews.append({'path':str(destination),'bytes':s.st_size,'sha256':inventory[-1]['sha256']})
        if sum(r['name'].lower().endswith('.czi') for r in inventory)!=1:raise SafetyError('Expected one owned vendor CZI.')
        archive_path=path.with_suffix('.vendor.json')
        archive={'schema':1,'action':'retire_vendor_after_persisted_observation','capture':p,'evidence_path':str(path),
            'evidence_sha256':file_hash(path),'inventory':inventory,'vendor_metadata_base64':archives,
            'vendor_previews_preserved':previews,'status':'verified_pending_removal'}
        atomic_json(archive_path,archive);durable(archive_path)
        if {f.name for f in folder.iterdir()}!={r['name'] for r in inventory}:raise SafetyError('Vendor observation inventory changed.')
        for item in inventory:
            child=folder/item['name'];s=child.stat()
            if redirected(child) or s.st_size!=item['bytes'] or s.st_mtime_ns!=item['mtime_ns']:raise SafetyError('Vendor observation changed before release.')
        for item in inventory:(folder/item['name']).unlink()
        folder.rmdir();archive.update(status='retired_after_persisted_observation',retired_at=time.time());atomic_json(archive_path,archive)
        receipt={'status':'removed','kind':'persisted_observation','vendor_result_folder':str(folder),
            'evidence':str(path),'archive':str(archive_path),'removed_bytes':sum(r['bytes'] for r in inventory)}
        record['receipt']=receipt
        return dict(receipt)
