"""Temporary synthetic vendor copies only; no microscope or real ZEN folders."""
import copy,json,threading,uuid
from types import SimpleNamespace as N

import cv2,numpy as np,pytest

import flakefinder.zen as zen
from flakefinder.imaging import save_frame
from flakefinder.model import Frame,Position,SafetyError


@pytest.fixture
def persisted(tmp_path,monkeypatch):
    root=tmp_path/'workspace/data/zen-captures';root.mkdir(parents=True)
    monkeypatch.setattr(zen,'_VENDOR_CAPTURE_ROOT',root)
    scope=zen.ZenMicroscope.__new__(zen.ZenMicroscope)
    scope.job_output_dir=root.resolve();scope._initialize_vendor_retention(False)
    folder=root/uuid.uuid4().hex;folder.mkdir()
    for name,data in [('image.czi',b'synthetic vendor container'),('camera.czjob',b'<Job/>'),
                      ('image.czi.metadata',b'<Metadata/>'),('thumb.png',b'preview')]:
        (folder/name).write_bytes(data)
    frame=Frame(np.arange(16*20*3,dtype=np.uint16).reshape(16,20,3),Position(1,2,3),(.1725,.1725),7,123.,
                {'camera_resources':{'ff_exposure_ms':200},'objective_position':2,'white_level':4095})
    identifier=scope._register_vendor_job(folder);scope._stamp_vendor_frame(identifier,frame);scope._mark_vendor_unloaded(identifier,frame)
    save_frame(tmp_path/'science','raw',frame)
    return scope,frame,tmp_path/'science/raw.tiff',folder


def test_opt_in_cleanup_requires_raw_save_and_archives_vendor_metadata(persisted):
    scope,frame,path,folder=persisted
    raw=path.read_bytes();sidecar=path.with_suffix('.json').read_bytes()
    result=scope.ack_persisted_frame(frame,path)
    assert result['status']=='removed' and not folder.exists()
    assert path.read_bytes()==raw and path.with_suffix('.json').read_bytes()==sidecar
    archive=json.loads(path.with_suffix('.zen-vendor.json').read_text())
    assert archive['status']=='removed_after_verified_raw_save'
    assert archive['vendor_metadata_base64']['camera.czjob']
    assert {r['name'] for r in archive['inventory']}=={'image.czi','camera.czjob','image.czi.metadata','thumb.png'}
    assert scope.ack_persisted_frame(frame,path)==result # Idempotent after the same verified save.


def test_default_retention_never_deletes_even_without_a_saved_frame(persisted):
    scope,frame,path,folder=persisted;scope.keep_vendor_results=True
    assert scope.ack_persisted_frame(frame,path.parent/'missing.tiff')['status']=='retained'
    assert folder.exists()


@pytest.mark.parametrize('change',['crop','average','saved_pixels','sidecar','unsaved','incomplete','foreign_session','unknown_file'])
def test_missing_or_invalid_acknowledgement_keeps_vendor_copy(persisted,change):
    scope,frame,path,folder=persisted
    if change=='crop':frame=Frame(frame.rgb[:8].copy(),frame.position,frame.pixel_size_um,frame.sequence,frame.acquired_at,copy.deepcopy(frame.metadata))
    elif change=='average':frame.rgb=frame.rgb.copy();frame.rgb[0,0,0]+=1
    elif change=='saved_pixels':
        altered=frame.rgb.copy();altered[0,0,0]+=1;cv2.imwrite(str(path),altered[:,:,::-1])
    elif change=='sidecar':
        sidecar=path.with_suffix('.json');data=json.loads(sidecar.read_text());data['metadata']['camera_resources']['ff_exposure_ms']=100
        sidecar.write_text(json.dumps(data))
    elif change=='unsaved':path=path.parent/'missing.tiff'
    elif change=='incomplete':scope._vendor_jobs[folder.name]['unloaded']=False
    elif change=='foreign_session':scope._vendor_session_id=uuid.uuid4().hex
    elif change=='unknown_file':(folder/'other-user-data.txt').write_text('Do not remove')
    with pytest.raises((SafetyError,FileNotFoundError)):scope.ack_persisted_frame(frame,path)
    assert folder.exists() and (folder/'image.czi').exists()


def test_unowned_folder_and_other_root_cannot_be_cleaned(persisted,tmp_path):
    scope,frame,path,folder=persisted
    record=scope._vendor_jobs[folder.name];record['folder']=str(tmp_path/'outside')
    with pytest.raises(SafetyError,match='owned UUID'):scope.ack_persisted_frame(frame,path)
    assert folder.exists()
    scope.job_output_dir=tmp_path/'outside'
    with pytest.raises(SafetyError,match='restricted'):scope._initialize_vendor_retention(False)


def test_nested_folder_or_reparse_entry_is_retained(persisted,monkeypatch):
    scope,frame,path,folder=persisted
    (folder/'unexpected').mkdir()
    with pytest.raises(SafetyError,match='subdirectory'):scope.ack_persisted_frame(frame,path)
    (folder/'unexpected').rmdir()
    original=zen._reparse_point
    monkeypatch.setattr(zen,'_reparse_point',lambda value:True if value.name=='image.czi' else original(value))
    with pytest.raises(SafetyError,match='link'):scope.ack_persisted_frame(frame,path)
    assert (folder/'image.czi').exists()


def test_successful_capture_is_acknowledgeable_only_after_unload(persisted,monkeypatch):
    scope,frame,_,_=persisted
    # Mock all vendor boundaries; this exercises only capture bookkeeping.
    scope.position=lambda:frame.position;scope.metadata=();scope.timeout_s=1
    scope._calls=set();scope._calls_lock=threading.Lock();scope.camera_resources={};scope.expected_scale=None;scope._mtb=None
    scope.check_cancel=lambda:None
    class Stream:
        def __iter__(self):return iter([N(frame_data=None)])
        def cancel(self):pass
    stream=Stream();future=N(result=lambda **kwargs:None,cancel=lambda:None)
    # SimpleNamespace is unhashable; the API tracks pending future objects.
    class Future:
        def result(self,**kwargs):pass
        def cancel(self):pass
    future=Future()
    scope.stream=N(MonitorAllExperiments=lambda *args,**kwargs:stream)
    scope.stream_pb=N(ExperimentStreamingServiceMonitorAllExperimentsRequest=N)
    pb=N(WorkflowServiceLoadJobTemplateRequest=N,WorkflowServiceRunJobRequest=N,
         WorkflowServiceIsJobTemplateLoadedRequest=N,WorkflowServiceUnloadJobTemplateRequest=N)
    scope._service=lambda *args:(pb,N(RunJob=N(future=lambda *args,**kwargs:future)))
    scope._rpc=lambda stub,method,request,**kwargs:N(is_job_template_loaded=False)
    scope.job_template='Synthetic Camera'
    monkeypatch.setattr(zen,'decode_frame',lambda data:copy.deepcopy(frame))
    monkeypatch.setattr(zen.time,'sleep',lambda seconds:None)
    captured=scope._capture_job()
    provenance=captured.metadata['zen_vendor_result'];record=scope._vendor_jobs[provenance['capture_id']]
    assert provenance['job_completed'] and provenance['job_unloaded']
    assert record['completed'] and record['unloaded']
    assert record['original_metadata']['zen_vendor_result']==provenance


def test_low_space_stops_before_job_directory_or_any_camera_rpc(tmp_path,monkeypatch):
    scope=zen.ZenMicroscope.__new__(zen.ZenMicroscope)
    scope.job_output_dir=tmp_path/'not-created'
    monkeypatch.setattr(zen.shutil,'disk_usage',lambda path:N(free=zen.CAPTURE_DISK_RESERVE_BYTES))
    scope.position=lambda:pytest.fail('Low-space capture queried hardware')
    scope._service=lambda *a:pytest.fail('Low-space capture opened camera service')
    with pytest.raises(zen.AcquisitionError,match='storage reserve'):
        scope._capture_job()
    assert not scope.job_output_dir.exists()


def test_capture_storage_guard_rechecks_every_invocation(tmp_path,monkeypatch):
    scope=zen.ZenMicroscope.__new__(zen.ZenMicroscope);scope.job_output_dir=tmp_path/'vendor'
    free=zen.CAPTURE_DISK_RESERVE_BYTES+zen.CAPTURE_DISK_ALLOWANCE_BYTES
    values=iter([free,free-1]);calls=[]
    def usage(path):calls.append(path);return N(free=next(values))
    monkeypatch.setattr(zen.shutil,'disk_usage',usage)
    assert scope._check_capture_storage()['next_capture_allowance_bytes']==512*1024**2
    with pytest.raises(zen.AcquisitionError):scope._check_capture_storage()
    assert len(calls)==2 and not scope.job_output_dir.exists()
