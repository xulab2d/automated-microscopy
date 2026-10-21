"""ZEISS 3.12 stage/focus and camera-job adapter using installed vendor contracts.

No microscope initialization, homing, or objective changes occur at connection.
The primary acquisition route uses a registered camera-only job in API Mode.
Without a job name, capture monitors an already running ZEN live stream.
"""
from __future__ import annotations
import base64,hashlib,importlib,os,shutil,stat,sys,threading,time,uuid
from dataclasses import asdict
from pathlib import Path
import json
import numpy as np
from .model import Position, Frame, Limits, SafetyError, AcquisitionError, Cancelled,atomic_json

_VENDOR_CAPTURE_ROOT=Path(__file__).resolve().parents[1]/'data/zen-captures'
CAPTURE_DISK_RESERVE_BYTES=10*1024**3
CAPTURE_DISK_ALLOWANCE_BYTES=512*1024**2


def _file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def _reparse_point(path):
    info=Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)


def _plain(value):return json.loads(json.dumps(value,sort_keys=True))

class PositionToleranceError(AcquisitionError):
    """A completed ZEISS motion RPC reported its own positioning tolerance failure."""

XY_RETRY_MAX_RPC_SECONDS = 1.0
XY_RETRY_WINDOW_SECONDS = 60.0
XY_RETRY_WINDOW_LIMIT = 3

class ZenMicroscope:
    from .vendor_lifecycle import pending_frame as vendor_frame_pending
    from .vendor_lifecycle import ack_observed as ack_observed_frame
    from .vendor_lifecycle import check_pending as _check_pending_vendor_output
    def __init__(self, endpoint='localhost:5002', gateway_dir=r'C:\ProgramData\Carl Zeiss\ZenApiGateway',
                 bindings_dir=None, timeout_s=15, settle_s=0.2, discard_frames=2,
                 job_template=None, job_output_dir='data/zen-captures',xy_backlash_um=0,stage_api_version='v1',camera_resources=None,
                 expected_objective_position=None,camera_white_level=4095,keep_vendor_results=True,
                 processing_overlap=False,processing_overlap_acceptance=None):
        from .storage import prepare_acquisition_storage, resolve_output_path
        # Storage and the operator hold are checked BEFORE any hardware client.
        job_output_dir = prepare_acquisition_storage(job_output_dir)
        from .capture_overlap import validate_acceptance
        self.processing_overlap=bool(processing_overlap)
        self.processing_overlap_acceptance=validate_acceptance(processing_overlap_acceptance,
            job_template,camera_resources or {},expected_objective_position,camera_white_level) if processing_overlap else None
        self._inside_frame_processor=False
        import grpc
        self.grpc=grpc
        from .lease import HardwareLease
        self.lease=HardwareLease(endpoint,Path(__file__).resolve().parents[1]/'data/locks')
        generated=Path(bindings_dir) if bindings_dir else Path(__file__).resolve().parents[1]/'generated'
        if not generated.exists():
            raise AcquisitionError('Run scripts/generate_zen_api.py first.')
        sys.path.insert(0,str(generated))
        folder=Path(gateway_dir)
        ca=(folder/'Certificates/ZenApiPersonalSigningRootCA.pem').read_bytes()
        token=(folder/'GlobalControlToken.txt').read_text(encoding='utf-8-sig').strip()
        if not token: raise AcquisitionError('ZEISS control token is empty.')
        self.channel=grpc.secure_channel(endpoint,grpc.ssl_channel_credentials(ca),
            options=[('grpc.max_receive_message_length',128*1024*1024)])
        self.metadata=(('control-token',token),)
        self.timeout_s=timeout_s
        self.settle_s=settle_s
        self.discard_frames=discard_frames
        if not np.isfinite(xy_backlash_um) or not 0<=xy_backlash_um<=100:
            raise ValueError('XY approach distance must be in [0,100] ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âµm.')
        self.xy_backlash_um=float(xy_backlash_um)
        self.job_template=job_template
        self.camera_resources=dict(camera_resources or {})
        self.expected_objective_position=expected_objective_position
        self.camera_white_level=float(camera_white_level)
        if not 0<self.camera_white_level<=65535:raise ValueError('Invalid sensor white level.')
        self._mtb=None
        self.job_output_dir=Path(job_output_dir).resolve()
        self._initialize_vendor_retention(keep_vendor_results)
        self.cancel=threading.Event()
        self.limits=None
        self.armed=False
        self.expected_scale=None
        self.motion_log=resolve_output_path(Path(__file__).resolve().parents[1]/'data/motion.jsonl')
        self._calls=set()
        self._calls_lock=threading.Lock()
        self._motion_lock=threading.Lock()
        if stage_api_version not in ('v1','v2'):raise ValueError('Stage API version must be v1 or v2.')
        self.stage_api_version=stage_api_version
        self.stage_um_per_unit=1.0 if stage_api_version=='v1' else 1e6
        self.stage_pb,self.stage=self._service(f'zen_api.lm.hardware.{stage_api_version}.stage_service','StageService')
        self.focus_pb,self.focus=self._service('zen_api.lm.hardware.v2.focus_service','FocusService')
        self.stream_pb,self.stream=self._service('zen_api.acquisition.v1beta.experiment_streaming_service','ExperimentStreamingService')

    def _initialize_vendor_retention(self,keep_vendor_results):
        if type(keep_vendor_results) is not bool:raise ValueError('keep_vendor_results must be a boolean.')
        self.keep_vendor_results=keep_vendor_results
        self._vendor_session_id=uuid.uuid4().hex
        self._vendor_jobs={};self._vendor_storage_lock=threading.Lock()
        from .storage import resolve_output_path
        # Pin the same managed, identity-verified path that acquisition uses.
        # This does not authorize deletion of any existing/foreign session.
        self._vendor_logical_root=_VENDOR_CAPTURE_ROOT
        self._vendor_capture_root=resolve_output_path(_VENDOR_CAPTURE_ROOT)
        if not keep_vendor_results and self.job_output_dir!=self._vendor_capture_root:
            raise SafetyError('Vendor cleanup is restricted to the managed project data/zen-captures directory.')

    def _register_vendor_job(self,folder):
        identifier=folder.name
        if uuid.UUID(hex=identifier).hex!=identifier:raise SafetyError('Invalid adapter result-folder identifier.')
        self._vendor_jobs[identifier]={'folder':str(folder.resolve()),'completed':False,'unloaded':False}
        return identifier

    def _check_capture_storage(self):
        """Stop before a new job if its volume cannot retain a bounded next capture.

        The 512 MiB allowance covers this adapter's 128 MiB stream-message
        ceiling plus vendor/raw/preview copies. This is rechecked for every
        job, including autofocus jobs whose vendor data remains retained.
        Concurrent external disk writers remain outside this guarantee.
        """
        from .storage import load_policy, project_root, verify_project
        policy=load_policy()
        if policy and self.job_output_dir.is_relative_to(project_root(policy)):
            # A cloud quota readback alone does not bound the local upload cache.
            verify_project(policy,acquisition=True)
        existing=self.job_output_dir.resolve()
        while not existing.exists():existing=existing.parent
        free=int(shutil.disk_usage(existing).free)
        required=CAPTURE_DISK_RESERVE_BYTES+CAPTURE_DISK_ALLOWANCE_BYTES
        if free<required:
            raise AcquisitionError(f'Capture storage reserve exhausted: {free} bytes free; {required} required before another ZEN job. No new result folder was created.')
        return {'checked_volume_path':str(existing),'free_bytes_before_capture':free,
                'reserve_bytes':CAPTURE_DISK_RESERVE_BYTES,'next_capture_allowance_bytes':CAPTURE_DISK_ALLOWANCE_BYTES,
                'policy':'checked_before_each_vendor_job; no automatic deletion of previous captures'}

    def _stamp_vendor_frame(self,identifier,frame,completed=True):
        record=self._vendor_jobs[identifier]
        pixels=np.ascontiguousarray(frame.rgb)
        provenance={'schema':1,'capture_id':identifier,'adapter_session_id':self._vendor_session_id,
            'result_folder':record['folder'],'raw_rgb_sha256':hashlib.sha256(pixels.tobytes()).hexdigest(),
            'raw_shape':list(pixels.shape),'raw_dtype':str(pixels.dtype),'job_completed':bool(completed),'job_unloaded':False}
        frame.metadata['zen_vendor_result']=provenance
        record.update(completed=bool(completed),provenance=_plain(provenance),position=asdict(frame.position),
            pixel_size_um=list(frame.pixel_size_um),sequence=frame.sequence,acquired_at=frame.acquired_at)

    def _mark_vendor_unloaded(self,identifier,frame):
        record=self._vendor_jobs[identifier]
        record['unloaded']=True;record['provenance']['job_unloaded']=True
        frame.metadata['zen_vendor_result']['job_unloaded']=True
        record['original_metadata']=_plain(frame.metadata)

    def ack_persisted_frame(self,frame,tiff_path):
        """Optionally remove one owned vendor copy AFTER a durable raw save.

        Never called automatically by capture. Crops, averages, modified pixels,
        missing/altered metadata, incomplete jobs, other sessions, and arbitrary
        folders cannot qualify. The TIFF/sidecar are never rewritten or deleted.
        A small vendor-metadata archive and byte inventory remain beside them.
        """
        if self.keep_vendor_results:return {'status':'retained','reason':'keep_vendor_results is enabled'}
        from .imaging import load_frame
        with self._vendor_storage_lock:
            provenance=frame.metadata.get('zen_vendor_result',{})
            identifier=provenance.get('capture_id');record=self._vendor_jobs.get(identifier)
            if record is None or provenance.get('adapter_session_id')!=self._vendor_session_id:
                raise SafetyError('Vendor result was not created by this adapter session.')
            if not record['completed'] or not record['unloaded'] or 'original_metadata' not in record:
                raise SafetyError('Vendor job has not completed and unloaded successfully.')
            if _plain(provenance)!=record['provenance']:raise SafetyError('Vendor capture provenance changed.')
            from .storage import resolve_output_path
            root=self._vendor_capture_root
            # Refuse policy/root changes since this adapter acquired ownership.
            # No Drive or local fallback is permitted during acknowledgement.
            if (root!=resolve_output_path(_VENDOR_CAPTURE_ROOT) or self.job_output_dir!=root
                    or root.absolute()!=root.resolve() or _reparse_point(root)):
                raise SafetyError('Vendor cleanup root changed or is redirected.')
            folder=Path(record['folder'])
            if folder.parent!=root.resolve() or folder.name!=identifier or uuid.UUID(hex=identifier).hex!=identifier:
                raise SafetyError('Vendor cleanup target is not the owned UUID directory.')
            target=Path(tiff_path).resolve();sidecar=target.with_suffix('.json')
            if target.suffix.lower() not in ('.tiff','.tif') or target.is_relative_to(root.resolve()):
                raise SafetyError('Durable raw TIFF must be outside the temporary vendor-result root.')
            expected=record['provenance']
            def validate_pixels(value):
                if list(value.rgb.shape)!=expected['raw_shape'] or str(value.rgb.dtype)!=expected['raw_dtype']:
                    raise SafetyError('Only the original full-frame raw image can acknowledge vendor cleanup.')
                if hashlib.sha256(np.ascontiguousarray(value.rgb).tobytes()).hexdigest()!=expected['raw_rgb_sha256']:
                    raise SafetyError('Raw image checksum differs from the original captured full frame.')
                for key,actual in (('position',asdict(value.position)),('pixel_size_um',list(value.pixel_size_um)),
                                   ('sequence',value.sequence),('acquired_at',value.acquired_at)):
                    if _plain(actual)!=_plain(record[key]):raise SafetyError('Raw image acquisition metadata changed: '+key)
                for key,original in record['original_metadata'].items():
                    if _plain(value.metadata.get(key))!=original:raise SafetyError('Original camera metadata changed or is missing: '+key)
            validate_pixels(frame)
            # Flush existing files, then read their contents back; this method
            # must not discard the vendor copy merely because save returned.
            for path in (target,sidecar):
                with path.open('rb+') as stream:os.fsync(stream.fileno())
            persisted=load_frame(target);validate_pixels(persisted)
            if 'receipt' in record:
                if record['receipt']['raw_tiff']!=str(target):raise SafetyError('Vendor copy was already acknowledged at another raw destination.')
                return dict(record['receipt'])
            if not folder.is_dir() or _reparse_point(folder) or folder.resolve()!=folder:
                raise SafetyError('Owned vendor result directory is absent or redirected.')
            inventory=[];archives={};archive_bytes=0
            for path in sorted(folder.iterdir()):
                if _reparse_point(path) or not path.is_file():raise SafetyError('Vendor folder contains a link or unexpected subdirectory; retaining it.')
                if path.suffix.lower() not in ('.czi','.czjob','.png','.metadata'):
                    raise SafetyError('Vendor folder contains an unrecognized result type; retaining it.')
                size=path.stat().st_size
                inventory.append({'name':path.name,'bytes':size,'sha256':_file_sha256(path),'mtime_ns':path.stat().st_mtime_ns})
                if path.suffix.lower() in ('.czjob','.metadata'):
                    archive_bytes+=size
                    if archive_bytes>4*1024*1024:raise SafetyError('Vendor metadata archive exceeds its bounded size; retaining results.')
                    archives[path.name]=base64.b64encode(path.read_bytes()).decode('ascii')
            if sum(item['name'].lower().endswith('.czi') for item in inventory)!=1:
                raise SafetyError('Expected exactly one vendor CZI for this acknowledged full frame.')
            archive_path=target.with_suffix('.zen-vendor.json')
            if archive_path.exists():
                previous=json.loads(archive_path.read_text(encoding='utf-8'))
                if previous.get('capture')!=expected or previous.get('raw_tiff')!=str(target):
                    raise SafetyError('An unrelated vendor provenance archive already exists at the raw destination.')
            archive={'schema':1,'created_at':time.time(),'action':'verified_vendor_duplicate_removal',
                'capture':expected,'raw_tiff':str(target),'raw_tiff_sha256':_file_sha256(target),
                'raw_sidecar':str(sidecar),'raw_sidecar_sha256':_file_sha256(sidecar),
                'inventory':inventory,'vendor_metadata_base64':archives,'status':'verified_pending_removal'}
            atomic_json(archive_path,archive)
            # Recheck the exact flat inventory immediately before removal. No
            # recursive delete, traversal, glob, or unowned directory is used.
            if {p.name for p in folder.iterdir()}!={item['name'] for item in inventory}:
                raise SafetyError('Vendor folder contents changed before cleanup; retaining results.')
            for item in inventory:
                path=folder/item['name'];info=path.stat()
                if _reparse_point(path) or info.st_size!=item['bytes'] or info.st_mtime_ns!=item['mtime_ns']:
                    raise SafetyError('Vendor result changed before cleanup; retaining results.')
            for item in inventory:(folder/item['name']).unlink()
            folder.rmdir()
            archive.update(status='removed_after_verified_raw_save',removed_at=time.time())
            atomic_json(archive_path,archive)
            receipt={'status':'removed','raw_tiff':str(target),'vendor_result_folder':str(folder),
                'removed_bytes':sum(item['bytes'] for item in inventory),'archive':str(archive_path),'archive_sha256':_file_sha256(archive_path)}
            record['receipt']=receipt
            return dict(receipt)

    def _service(self,name,service):
        pb=importlib.import_module(name+'_pb2')
        return pb,getattr(importlib.import_module(name+'_pb2_grpc'),service+'Stub')(self.channel)

    def _assert_not_processing(self):
        if (getattr(self,'_inside_frame_processor',False) and
                getattr(self,'_frame_processor_thread_id',None)==threading.get_ident()):
            raise SafetyError('Early-frame processing must not invoke hardware APIs.')

    def _rpc(self,stub,method,request,timeout=None):
        if method not in ('Stop','StopJob'):self._assert_not_processing()
        try:
            return getattr(stub,method)(request,metadata=self.metadata,timeout=timeout or self.timeout_s)
        except self.grpc.RpcError as e:
            raise AcquisitionError(f'ZEISS {method}: {e.code().name}: {e.details()}') from e

    def _motion_rpc(self,stub,request):
        self._assert_not_processing()
        # Serialize registration against Stop: a delayed worker cannot submit a
        # new motion after Stop has cancelled the operation.
        with self._calls_lock:
            self.check_cancel()
            future=stub.MoveTo.future(request,metadata=self.metadata,timeout=self.timeout_s)
            self._calls.add(future)
        try:
            return future.result()
        except self.grpc.RpcError as e:
            self.check_cancel()
            if e.code()==self.grpc.StatusCode.INTERNAL and e.details() in (
                    'Requested position was not reached.','Position could not be reached.'):
                raise PositionToleranceError(f'ZEISS MoveTo: {e.details()}') from e
            raise AcquisitionError(f'ZEISS MoveTo: {e.code().name}: {e.details()}') from e
        except self.grpc.FutureCancelledError:
            self.check_cancel()
            raise AcquisitionError('ZEISS motion request was cancelled.')
        finally:
            with self._calls_lock:self._calls.discard(future)

    def position(self):
        xy=self._rpc(self.stage,'GetPosition',self.stage_pb.StageServiceGetPositionRequest())
        z=self._rpc(self.focus,'GetPosition',self.focus_pb.FocusServiceGetPositionRequest())
        scale=getattr(self,'stage_um_per_unit',1e6)
        return Position(xy.x*scale,xy.y*scale,z.value*1e6)

    def arm(self,limits: Limits):
        self.lease.acquire()
        try:
            limits.check(self.position())
            if getattr(self,'expected_objective_position',None) is not None:
                self.hardware_controls().verify_optics(self.expected_objective_position)
        except BaseException:
            self.lease.close()
            raise
        self.limits=limits
        self.cancel.clear()
        self.armed=True

    def check_cancel(self):
        if self.cancel.is_set(): raise Cancelled('Acquisition stopped.')

    def move(self,target: Position):
        from google.protobuf.wrappers_pb2 import DoubleValue
        # The connected stage reports nanometre increments. Raster coordinates
        # often contain finer fractions that are not physically representable.
        target=Position(round(target.x,3),round(target.y,3),target.z)
        if getattr(self,'expected_objective_position',None) is not None:
            self.hardware_controls().verify_optics(self.expected_objective_position)
        with self._motion_lock:
            self.check_cancel()
            if not self.armed or self.limits is None: raise SafetyError('Motion is not armed.')
            start=self.position()
            self.limits.check_move(start,target)
            self._record_motion('request',start,target)
            try:
                # This ZEN adapter aborts a combined XY request after an X-axis
                # rounding warning, before Y starts. Address each optional axis
                # separately and verify it before submitting the next axis.
                for axis in ('x','y','z'):
                    current=self.position()
                    if abs(getattr(current,axis)-getattr(target,axis))<=self._axis_tolerance(axis):continue
                    values={'x':current.x,'y':current.y,'z':current.z};values[axis]=getattr(target,axis)
                    waypoint=Position(**values);self.limits.check_move(current,waypoint)
                    if axis=='z':
                        stub=self.focus;request=self.focus_pb.FocusServiceMoveToRequest(value=target.z/1e6)
                    else:
                        scale=getattr(self,'stage_um_per_unit',1e6)
                        stub=self.stage;request=self.stage_pb.StageServiceMoveToRequest(
                            **{axis:DoubleValue(value=getattr(waypoint,axis)/scale)})
                    self._move_axis(stub,request,axis,current,waypoint)
                    # Verify each axis before starting another, but apply the
                    # full optical dwell only once after the final axis.
                    self._settled_position(waypoint,optical_dwell=False)
                final=self._settled_position(target)
                self._record_motion('settled',final,target)
                return final
            except BaseException as exc:
                stop_errors=self.stop()
                try:self._record_motion('failed',self.position(),target,str(exc))
                except Exception:pass
                if stop_errors:raise AcquisitionError(str(exc)+'; STOP could not be confirmed: '+'; '.join(stop_errors)) from exc
                raise

    def _move_axis(self,stub,request,axis,start,target):
        """Retry one promptly rejected, stationary XY command in the same owner.

        A timeout, partial move, changing readback, or Z error is not this case.
        The retry repeats the identical absolute command; it never resets an
        adapter, changes limits, clears cancellation, or takes a detour.
        """
        for attempt in range(2):
            self.check_cancel()
            started=time.monotonic()
            try:
                response=self._motion_rpc(stub,request)
                self._record_motion('vendor_motion_response',self.position(),target,
                    details={'axis':axis,'attempt':attempt+1,
                             'position_changed':getattr(response,'position_changed',None)})
                return
            except PositionToleranceError as exc:
                elapsed=time.monotonic()-started
                p=self.position();self.limits.check(p)
                if self._target_reached(p,target):
                    self._record_motion('vendor_tolerance_warning',p,target,str(exc),
                        details={'axis':axis,'attempt':attempt+1})
                    return  # Caller still requires stationary target verification.
                self._record_motion('vendor_position_rejected',p,target,str(exc),
                    details={'axis':axis,'attempt':attempt+1,'rpc_elapsed_s':elapsed})
                if (attempt or axis not in ('x','y') or elapsed>XY_RETRY_MAX_RPC_SECONDS
                        or self._position_error(p,start)>=.05):
                    raise
                samples=[p]
                for _ in range(4):
                    self.cancel.wait(.05);self.check_cancel()
                    p=self.position();self.limits.check(p)
                    if (self._position_error(p,start)>=.05
                            or self._position_error(p,samples[-1])>=.05):
                        raise exc
                    samples.append(p)
                self.check_cancel()
                self.limits.check_move(p,target)
                if getattr(self,'expected_objective_position',None) is not None:
                    self.hardware_controls().verify_optics(self.expected_objective_position)
                now=time.monotonic()
                recent=[t for t in getattr(self,'_xy_retry_times',[])
                        if now-t<XY_RETRY_WINDOW_SECONDS]
                if len(recent)>=XY_RETRY_WINDOW_LIMIT:raise
                self._xy_retry_times=recent+[now]
                self._record_motion('stationary_xy_retry',p,target,str(exc),details={
                    'axis':axis,'attempt':2,'rpc_elapsed_s':elapsed,
                    'stationary_readbacks':[asdict(s) for s in samples],
                    'retries_in_window':len(self._xy_retry_times)})

    @staticmethod
    def _position_error(a,b):return max(abs(a.x-b.x),abs(a.y-b.y),abs(a.z-b.z))

    def _axis_tolerance(self,axis):
        return getattr(self,'focus_target_tolerance_um',.12) if axis=='z' else .3

    def _target_reached(self,a,b):
        return all(abs(getattr(a,axis)-getattr(b,axis))<=self._axis_tolerance(axis) for axis in ('x','y','z'))

    def _settled_position(self,target,optical_dwell=True):
        deadline=time.monotonic()+self.timeout_s;stable=0;previous=None;p=None
        while time.monotonic()<deadline:
            self.check_cancel();p=self.position()
            self.limits.check(p)
            if self._target_reached(p,target):
                stationary=previous is not None and self._position_error(p,previous)<.05
                stable=stable+1 if stationary else 1
                if stable>=3:
                    if optical_dwell and self.cancel.wait(self.settle_s):self.check_cancel()
                    final=self.position()
                    self.limits.check(final)
                    if not self._target_reached(final,target):raise AcquisitionError('Motion drifted outside tolerance during settling.')
                    return final
            else:stable=0
            previous=p;self.cancel.wait(.05)
        raise AcquisitionError(f'Motion readback did not settle at target {target}; last readback {p}; '
                               f'XY tolerance 0.3 um, Z tolerance {self._axis_tolerance("z"):.6g} um.')

    def _record_motion(self,event,position,target,error=None,details=None):
        if not hasattr(self,'motion_log'):return
        from dataclasses import asdict
        self.motion_log.parent.mkdir(parents=True,exist_ok=True)
        with self.motion_log.open('a',encoding='utf-8') as file:
            file.write(json.dumps({'time':time.time(),'event':event,'position':asdict(position),
                                   'target':asdict(target),'error':error,
                                   'focus_motion_spec':getattr(self,'focus_motion_spec',None),
                                   **(details or {})})+'\n')

    def move_z(self,z):
        p=self.position()
        return self.move(Position(p.x,p.y,float(z)))

    def travel(self,x,y):
        """Bounded XY travel, optionally approaching both axes from below."""
        self.check_cancel()
        p=self.position()
        if self.limits is None or not self.armed: raise SafetyError('Motion is not armed.')
        self.limits.check(Position(float(x),float(y),p.z))
        if self.xy_backlash_um:
            # Only preload an axis that needs reversal or a short approach.
            # An unchanged axis must not make a needless 60-um round trip.
            # A sufficiently long positive move already takes up the backlash.
            pre=tuple(getattr(p,axis) if abs(value-getattr(p,axis))<=self._axis_tolerance(axis)
                      or value-getattr(p,axis)>=self.xy_backlash_um
                      else value-self.xy_backlash_um
                      for axis,value in (('x',float(x)),('y',float(y))))
            self.limits.check(Position(*pre,p.z))
            self._travel_direct(*pre)
        return self._travel_direct(x,y)

    def _travel_direct(self,x,y):
        """Use bounded axis segments, avoiding tiny diagonal-axis corrections."""
        self.check_cancel()
        p=self.position()
        if self.limits is None or not self.armed: raise SafetyError('Motion is not armed.')
        self.limits.check(Position(float(x),float(y),p.z))
        for axis,value in [('x',x),('y',y)]:
            start=self.position();distance=value-getattr(start,axis)
            if abs(distance)<=self._axis_tolerance(axis):continue
            n=max(1,int(np.ceil(abs(distance)/max(self.limits.max_xy_step_um-.002,.001))))
            for i in range(1,n+1):
                v={'x':start.x,'y':start.y,'z':start.z};v[axis]=getattr(start,axis)+distance*i/n
                self.move(Position(**v))

    def _capture_memory_headroom(self):
        from .acquisition_resources import wait_for_memory_headroom
        managed=getattr(self,'capture_memory_check',None)
        if managed is not None:return managed()
        return wait_for_memory_headroom(self.check_cancel,self.cancel.wait)

    def capture(self):
        """Discard in-flight images, require actual frame XYZ to match settled readback."""
        self.check_cancel()
        if self.job_template:
            if hasattr(self,'lease'):self.lease.acquire()
            return self._capture_job()
        memory_guard=self._capture_memory_headroom()
        target=self.position()
        call=self.stream.MonitorAllExperiments(self.stream_pb.ExperimentStreamingServiceMonitorAllExperimentsRequest(
            enable_raw_data=False),metadata=self.metadata,timeout=self.timeout_s)
        with self._calls_lock: self._calls.add(call)
        seen=0
        try:
            for response in call:
                self.check_cancel()
                seen+=1
                if seen<=self.discard_frames: continue
                f=decode_frame(response.frame_data)
                f.metadata['capture_memory_guard']=memory_guard
                if max(abs(f.position.x-target.x),abs(f.position.y-target.y),abs(f.position.z-target.z))>0.5:
                    continue
                if self.expected_scale is not None and not np.allclose(f.pixel_size_um,self.expected_scale,rtol=0.01):
                    raise SafetyError('Objective/camera scale changed during acquisition.')
                return f
            raise AcquisitionError('ZEISS camera stream ended without a fresh matching frame.')
        except self.grpc.RpcError as e:
            self.check_cancel()
            raise AcquisitionError(f'Camera stream: {e.code().name}: {e.details()}. Start Live in ZEN.') from e
        finally:
            call.cancel()
            with self._calls_lock: self._calls.discard(call)

    def capture_with_processing(self,processor):
        """Overlap pure CPU work only; return after job success and unload fence.

        Without stationary qualification this is exactly sequential capture.
        A processor exception uses the same owned-job cancellation/cleanup as
        acquisition errors. No provisional frame is returned to the caller.
        """
        if not callable(processor):raise ValueError('A pure frame processor is required.')
        self.check_cancel()
        if not getattr(self,'processing_overlap',False) or not self.job_template:
            frame=self.capture();return frame,processor(frame)
        if not getattr(self,'processing_overlap_acceptance',None):raise SafetyError('Processing overlap lacks stationary acceptance.')
        if hasattr(self,'lease'):self.lease.acquire()
        return self._capture_job(processor)

    def _capture_job(self,frame_processor=None):
        """Acquire through a registered camera-only job in supervised ZEN core API Mode."""
        import uuid
        started=time.perf_counter();timings={}
        self._check_pending_vendor_output()
        storage_guard=self._check_capture_storage()
        memory_guard=self._capture_memory_headroom()
        expected_objective=getattr(self,'expected_objective_position',None)
        if expected_objective is not None:self.hardware_controls().verify_optics(expected_objective)
        target=self.position()
        pb,workflow=self._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
        result_folder=self.job_output_dir/uuid.uuid4().hex
        result_folder.mkdir(parents=True)
        vendor_identifier=self._register_vendor_job(result_folder)
        self._rpc(workflow,'LoadJobTemplate',pb.WorkflowServiceLoadJobTemplateRequest(
            job_template_name=self.job_template,result_path=str(result_folder)))
        timings['job_load']=time.perf_counter()-started;resources_started=time.perf_counter()
        call=self.stream.MonitorAllExperiments(self.stream_pb.ExperimentStreamingServiceMonitorAllExperimentsRequest(
            enable_raw_data=False),metadata=self.metadata,timeout=self.timeout_s)
        with self._calls_lock: self._calls.add(call)
        future=None;capture_succeeded=False
        try:
            settings={}
            if getattr(self,'camera_resources',{}):
                resource_pb,resources=self._service('zen_api.workflows.v1beta.job_resources_service','JobResourcesService')
                for name,value in self.camera_resources.items():
                    if name=='ff_exposure_ms':
                        if not np.isfinite(value) or not 1<=value<=10000:raise ValueError('Exposure must be 1..10000 ms.')
                        self._rpc(resources,'SetDoubleValue',resource_pb.JobResourcesServiceSetDoubleValueRequest(resource_id=name,value=float(value)))
                        actual=self._rpc(resources,'GetDoubleValue',resource_pb.JobResourcesServiceGetDoubleValueRequest(resource_id=name)).value
                        if abs(actual-value)>1e-5:raise AcquisitionError('Camera job exposure resource readback mismatch.')
                    elif name=='ff_white_balance':
                        channels=np.asarray([float(v) for v in str(value).split(',')])
                        if channels.shape!=(3,) or not np.isfinite(channels).all() or np.any(channels<=0) or np.any(channels>10):
                            raise ValueError('White balance requires three finite positive vendor coefficients no larger than 10.')
                        value=','.join(format(float(v),'.12g') for v in channels)
                        self._rpc(resources,'SetStringValue',resource_pb.JobResourcesServiceSetStringValueRequest(resource_id=name,value=value))
                        actual=self._rpc(resources,'GetStringValue',resource_pb.JobResourcesServiceGetStringValueRequest(resource_id=name)).value
                        if actual!=value:raise AcquisitionError('Camera job white balance resource readback mismatch.')
                    else:raise ValueError('Unsupported camera resource.')
                    settings[name]=actual
            timings['resource_setup']=time.perf_counter()-resources_started
            acquisition_started=time.perf_counter()
            # The subscriber is created before the acquisition. A separate RPC future allows
            # the stream consumer and the camera job to make progress concurrently.
            time.sleep(0.1)
            timings['subscription_ready_wait']=time.perf_counter()-acquisition_started
            run_requested=time.perf_counter()
            with self._calls_lock:
                self.check_cancel()
                future=workflow.RunJob.future(pb.WorkflowServiceRunJobRequest(),metadata=self.metadata,timeout=self.timeout_s)
                self._calls.add(future)
            frame=None
            for response in call:
                received=time.perf_counter()
                timings['request_to_first_image']=received-run_requested
                self.check_cancel()
                candidate=decode_frame(response.frame_data)
                timings['image_decode']=time.perf_counter()-received
                if max(abs(candidate.position.x-target.x),abs(candidate.position.y-target.y),abs(candidate.position.z-target.z))>0.5:
                    raise SafetyError('Acquisition job changed XYZ or returned an unexpected position.')
                frame=candidate
                break
            if frame_processor is None:
                completion_wait_started=time.perf_counter();future.result(timeout=self.timeout_s)
                timings['completion_after_image']=time.perf_counter()-completion_wait_started
                timings['camera_job_and_transfer']=time.perf_counter()-acquisition_started
            if frame is None: raise AcquisitionError('Camera job completed without an image.')
            if self.expected_scale is not None and not np.allclose(frame.pixel_size_um,self.expected_scale,rtol=0.01):
                raise SafetyError('Camera scale changed during acquisition.')
            frame.metadata['camera_resources']=settings
            frame.metadata['camera_job_template']=self.job_template
            frame.metadata['capture_storage_guard']=storage_guard
            frame.metadata['capture_memory_guard']=memory_guard
            frame.metadata['white_level']=getattr(self,'camera_white_level',4095)
            if getattr(self,'_mtb',None):
                state=self._mtb.verify_optics(expected_objective) if expected_objective is not None else self._mtb.objectives()
                frame.metadata['objective_position']=state['position']
                frame.metadata['objective']=next((o for o in state['objectives'] if o['position']==state['position']),None)
                frame.metadata['illumination_percent']=self._mtb.lamp_percent()
            timings['capture_total_before_cleanup']=time.perf_counter()-started
            frame.metadata['acquisition_timing_s']=timings
            self._stamp_vendor_frame(vendor_identifier,frame,completed=False)
            processed=None
            if frame_processor is not None:
                processing_started=time.perf_counter();self._inside_frame_processor=True;self._frame_processor_thread_id=threading.get_ident()
                was_writable=frame.rgb.flags.writeable;frame.rgb.flags.writeable=False
                try:processed=frame_processor(frame)
                finally:
                    frame.rgb.flags.writeable=was_writable
                    self._inside_frame_processor=False;self._frame_processor_thread_id=None
                timings['early_processing_s']=time.perf_counter()-processing_started
            if frame_processor is not None:
                completion_wait_started=time.perf_counter();future.result(timeout=self.timeout_s)
                timings['completion_after_image']=time.perf_counter()-completion_wait_started
                timings['camera_job_and_transfer']=time.perf_counter()-acquisition_started
            timings['capture_total_before_cleanup']=time.perf_counter()-started
            record=self._vendor_jobs[vendor_identifier];record['completed']=True
            record['provenance']['job_completed']=True;frame.metadata['zen_vendor_result']['job_completed']=True
            frame.metadata['processing_overlap']={'enabled':frame_processor is not None,
                'return_fence':'RunJob success and successful unload',
                'acceptance':getattr(self,'processing_overlap_acceptance',None)}
            capture_succeeded=True
            return (frame,processed) if frame_processor is not None else frame
        except BaseException:
            if future is not None: future.cancel()
            try: self._rpc(workflow,'StopJob',pb.WorkflowServiceStopJobRequest(),timeout=3)
            except Exception: pass
            raise
        finally:
            cleanup_started=time.perf_counter()
            call.cancel()
            with self._calls_lock:
                self._calls.discard(call)
                if future is not None: self._calls.discard(future)
            had_error=not capture_succeeded
            # ZEN core 3.12 unloads a successfully completed job automatically.
            # Other versions may keep it loaded. Query before explicit cleanup.
            try:
                loaded=self._rpc(workflow,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest(),timeout=3)
                if loaded.is_job_template_loaded:
                    self._rpc(workflow,'UnloadJobTemplate',pb.WorkflowServiceUnloadJobTemplateRequest(),timeout=3)
                    loaded=self._rpc(workflow,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest(),timeout=3)
                if loaded.is_job_template_loaded:raise AcquisitionError('Vendor camera job remained loaded after cleanup.')
                if not had_error:
                    timings['job_cleanup']=time.perf_counter()-cleanup_started
                    timings['capture_total_including_cleanup']=time.perf_counter()-started
                    self._mark_vendor_unloaded(vendor_identifier,frame)
            except Exception as e:
                self.armed=False
                if not had_error:raise AcquisitionError('Camera job cleanup failed: '+str(e)) from e

    def stop(self):
        """Does not acquire the movement lock: can interrupt another worker's RPC."""
        with self._calls_lock:
            self.cancel.set()
            self.armed=False
            for call in list(self._calls): call.cancel()
        errors=[]
        if hasattr(self,'lease') and self.lease.file is None:
            return ['Hardware STOP not sent: this client does not own the microscope lease.']
        for stub,pb,name in [(self.stage,self.stage_pb,'StageService'),(self.focus,self.focus_pb,'FocusService')]:
            try: self._rpc(stub,'Stop',getattr(pb,name+'StopRequest')(),timeout=3)
            except Exception as e: errors.append(str(e))
        if self.job_template:
            try:
                pb,stub=self._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
                loaded=self._rpc(stub,'IsJobTemplateLoaded',pb.WorkflowServiceIsJobTemplateLoadedRequest(),timeout=3)
                if loaded.is_job_template_loaded:self._rpc(stub,'StopJob',pb.WorkflowServiceStopJobRequest(),timeout=3)
            except Exception as e:errors.append(str(e))
        return errors

    def close(self):
        self.armed=False
        try:
            if getattr(self,'_mtb',None):self._mtb.close()
        finally:
            self._mtb=None
            try:self.channel.close()
            finally:self.lease.close()

    def hardware_controls(self):
        self._assert_not_processing()
        if self._mtb is None:
            from .mtb import MtbControls
            controls=MtbControls(self)
            try:
                spec=controls.focus_motion_spec()
                self.focus_target_tolerance_um=spec['max_deviation_um']+spec['readback_rounding_allowance_um']
                self.focus_motion_spec=spec
            except BaseException:
                controls.close()
                raise
            self._mtb=controls
        return self._mtb

    def set_objective(self,position,clearance_z_um):
        return self.hardware_controls().set_objective(position,clearance_z_um)

    def set_exposure_ms(self,value):
        self._assert_not_processing()
        if not np.isfinite(value) or not 1<=value<=10000:raise ValueError('Exposure must be 1..10000 ms.')
        self.camera_resources['ff_exposure_ms']=float(value)

def decode_frame(f):
    p=f.pixel_data
    w,h=p.size.width,p.size.height
    if w!=f.frame_size.width or h!=f.frame_size.height or p.start_position.x or p.start_position.y:
        raise AcquisitionError('Partial camera frames are not accepted.')
    formats={1:('u1',1),2:('<u2',1),4:('u1',3),5:('<u2',3)}
    if p.pixel_type not in formats: raise AcquisitionError(f'Unsupported pixel format {p.pixel_type}')
    dtype,channels=formats[p.pixel_type]
    if w<=0 or h<=0 or w*h*3*np.dtype(dtype).itemsize>128*1024**2:
        raise AcquisitionError('Decoded RGB frame exceeds the 128 MiB image budget or has invalid dimensions.')
    expected=w*h*channels*np.dtype(dtype).itemsize
    if len(p.raw_data)!=expected: raise AcquisitionError('Malformed camera frame byte count.')
    a=np.frombuffer(p.raw_data,dtype=dtype).reshape(h,w,channels)
    a=a[:,:,::-1].copy() if channels==3 else np.repeat(a,3,axis=2)
    pos=f.frame_stage_position
    if not all(pos.HasField(k) for k in ('x','y','z')):
        raise AcquisitionError('Frame has no complete XYZ metadata; refusing uncertain coordinates.')
    return Frame(a,Position(pos.x.value*1e6,pos.y.value*1e6,pos.z.value*1e6),
        (f.scaling.x*1e6,f.scaling.y*1e6),f.frame_number,
        metadata={'experiment_id':f.experiment_id,'pixel_type':p.pixel_type,'source':'ZEN API raw stream'})
