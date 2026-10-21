"""Installed RPC shape is mocked; no channel/client or microscope connection."""
import threading
from types import SimpleNamespace as N
import numpy as np
import pytest
import flakefinder.zen as zen
import flakefinder.storage as storage
from flakefinder.model import Frame,Position,AcquisitionError,SafetyError,Cancelled
from flakefinder.capture_overlap import validate_acceptance

@pytest.fixture
def job(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'load_policy',lambda:None)
    scope=zen.ZenMicroscope.__new__(zen.ZenMicroscope);scope.job_output_dir=tmp_path/'vendor'
    scope._initialize_vendor_retention(True);scope._mtb=None;scope.expected_scale=None
    scope.metadata=();scope.timeout_s=1;scope.camera_resources={};scope.job_template='Camera'
    scope._calls=set();scope._calls_lock=threading.Lock();scope.expected_objective_position=None;scope.camera_white_level=4095
    scope.check_cancel=lambda:None;scope.position=lambda:Position(1,2,3);scope._check_capture_storage=lambda:{}
    scope.processing_overlap=True;scope.processing_overlap_acceptance={'synthetic_fixture':True};scope._inside_frame_processor=False
    events=[];scope.failure=None
    class Future:
        def result(self,**kwargs):
            events.append('completion')
            if scope.failure:raise scope.failure
        def cancel(self):events.append('future_cancel')
    future=Future()
    class Stream:
        def __iter__(self):return iter([N(frame_data=None)])
        def cancel(self):events.append('stream_cancel')
    scope.stream=N(MonitorAllExperiments=lambda *a,**k:Stream())
    scope.stream_pb=N(ExperimentStreamingServiceMonitorAllExperimentsRequest=N)
    pb=N(WorkflowServiceLoadJobTemplateRequest=N,WorkflowServiceRunJobRequest=N,
         WorkflowServiceIsJobTemplateLoadedRequest=N,WorkflowServiceUnloadJobTemplateRequest=N,
         WorkflowServiceStopJobRequest=N)
    scope._service=lambda *a:(pb,N(RunJob=N(future=lambda *a,**k:future)))
    def rpc(stub,method,request,**kwargs):events.append(method);return N(is_job_template_loaded=False)
    scope._rpc=rpc
    frame=Frame(np.ones((8,9,3),np.uint16),scope.position(),(.1725,.1725),1)
    monkeypatch.setattr(zen,'decode_frame',lambda response:frame)
    monkeypatch.setattr(zen.time,'sleep',lambda seconds:None)
    return scope,events

def test_processor_runs_before_completion_but_returns_after_unload(job):
    scope,events=job
    def processor(frame):
        assert 'completion' not in events
        assert frame.metadata['zen_vendor_result']['job_completed'] is False
        events.append('processor');return 42
    frame,result=scope.capture_with_processing(processor)
    assert result==42 and events.index('processor')<events.index('completion')<events.index('IsJobTemplateLoaded')
    assert frame.metadata['zen_vendor_result']['job_completed'] and frame.metadata['zen_vendor_result']['job_unloaded']

@pytest.mark.parametrize('failure',[AcquisitionError('late camera failure'),Cancelled('operator STOP')])
def test_late_job_failure_or_stop_never_returns_processed_frame(job,failure):
    scope,events=job;scope.failure=failure
    with pytest.raises(type(failure),match=str(failure)):scope.capture_with_processing(lambda f:123)
    assert 'StopJob' in events and 'stream_cancel' in events and not scope._inside_frame_processor
    assert not any(r['unloaded'] for r in scope._vendor_jobs.values())

def test_processor_exception_still_cancels_owned_job(job):
    scope,events=job
    def processor(frame):raise ValueError('CPU processing failed')
    with pytest.raises(ValueError):scope.capture_with_processing(processor)
    assert 'future_cancel' in events and 'StopJob' in events

def test_unqualified_overlap_refuses_before_start(job):
    scope,events=job;scope.processing_overlap_acceptance=None
    with pytest.raises(SafetyError,match='stationary acceptance'):scope.capture_with_processing(lambda f:None)
    assert events==[]

def test_missing_acceptance_record_is_not_an_implicit_approval():
    with pytest.raises(SafetyError):validate_acceptance(None,'job',{},2,4095)

def test_early_processor_cannot_modify_raw_pixels(job):
    scope,events=job
    def processor(frame):frame.rgb[0,0,0]=17
    with pytest.raises(ValueError,match='read-only'):scope.capture_with_processing(processor)
    assert 'StopJob' in events

def test_processor_guard_is_thread_local_and_does_not_block_external_stop(job):
    scope,_=job;seen=[]
    def processor(frame):
        with pytest.raises(SafetyError):scope._assert_not_processing()
        thread=threading.Thread(target=lambda:(scope._assert_not_processing(),seen.append('external_control_allowed')))
        thread.start();thread.join();return 1
    scope.capture_with_processing(processor)
    assert seen==['external_control_allowed']
