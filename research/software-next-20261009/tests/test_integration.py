import json,threading
from pathlib import Path
from types import SimpleNamespace as N
from dataclasses import asdict
import numpy as np,pytest,grpc
from flakefinder.model import Position,Limits,CameraTransform,SafetyError,AcquisitionError,Cancelled
from flakefinder.zen import ZenMicroscope,decode_frame
from flakefinder.autofocus import evaluate_fine_curve,autofocus_range
from flakefinder.simulator import SimulatedMicroscope
from flakefinder.workflow import inset_polygon,scan_chips
from flakefinder.config import demo_config
from flakefinder.scan import ScanPlan,run_scan

def frame_packet():
    p=N(x=N(value=.010),y=N(value=.020),z=N(value=.003),HasField=lambda _:True)
    a=np.array([[[1,2,3],[4,5,6]],[[7,8,9],[10,11,12]]],np.uint16)
    return N(pixel_data=N(size=N(width=2,height=2),start_position=N(x=0,y=0),pixel_type=5,raw_data=a.tobytes()),
             frame_size=N(width=2,height=2),frame_stage_position=p,scaling=N(x=.3e-6,y=.3e-6),frame_number=7,experiment_id='test')

def test_zen_raw_decode_channel_order_units_and_partial_frame():
    packet=frame_packet();f=decode_frame(packet)
    assert f.position==Position(10000,20000,3000)
    assert f.rgb[0,0].tolist()==[3,2,1]
    assert f.pixel_size_um==(.3,.3)
    packet.pixel_data.raw_data=b'bad'
    with pytest.raises(AcquisitionError,match='byte count'):decode_frame(packet)
    packet=frame_packet();packet.pixel_data.start_position.x=1
    with pytest.raises(AcquisitionError,match='Partial'):decode_frame(packet)
    packet=frame_packet();packet.frame_stage_position.HasField=lambda k:k!='z'
    with pytest.raises(AcquisitionError,match='XYZ'):decode_frame(packet)

def bare_zen():
    s=ZenMicroscope.__new__(ZenMicroscope)
    s.grpc=grpc;s.metadata=();s.timeout_s=1;s.settle_s=0;s.cancel=threading.Event()
    s._calls_lock=threading.Lock();s._motion_lock=threading.Lock();s._calls=set();s.armed=True
    s.limits=Limits(0,20000,0,30000,2000,4000,100,10);s.expected_scale=None;s.job_template=None
    s.stage_pb=N(StageServiceMoveToRequest=lambda **kw:N(**kw),StageServiceStopRequest=lambda:N())
    s.focus_pb=N(FocusServiceMoveToRequest=lambda **kw:N(**kw),FocusServiceStopRequest=lambda:N())
    s.stage=N();s.focus=N();return s

def test_real_adapter_converts_motion_units_and_stops_both_axes():
    s=bare_zen();state=[Position(10000,20000,3000)];sent=[];stops=[]
    s.position=lambda:state[0]
    def move(stub,request):
        sent.append(request)
        if stub is s.stage:state[0]=Position(request.x.value*1e6 if hasattr(request,'x') else state[0].x,request.y.value*1e6 if hasattr(request,'y') else state[0].y,state[0].z)
        else:state[0]=Position(state[0].x,state[0].y,request.value*1e6)
    s._motion_rpc=move;s._rpc=lambda stub,method,request,timeout=None:stops.append((stub,method))
    s.move(Position(10010,20000,3000));s.move_z(3002)
    assert sent[0].x.value==pytest.approx(.01001)
    assert sent[1].value==pytest.approx(.003002)
    s.stop();assert len(stops)==2 and not s.armed
    with pytest.raises(Cancelled):s.move_z(3000)
    assert len(sent)==2

def test_zen_failed_motion_disarms_and_stops():
    s=bare_zen();s.position=lambda:Position(10000,20000,3000);stops=[]
    def fail(*_):raise AcquisitionError('readback lost')
    s._motion_rpc=fail;s._rpc=lambda *a,**kw:stops.append(a)
    with pytest.raises(AcquisitionError):s.move_z(3002)
    assert s.cancel.is_set() and not s.armed and len(stops)==2

def test_camera_job_auto_unload_keeps_motion_armed(tmp_path):
    s=bare_zen();s.position=lambda:Position(10000,20000,3000)
    s.job_template='camera-only';s.job_output_dir=tmp_path
    class Call:
        def __iter__(self):return iter([N(frame_data=frame_packet())])
        def cancel(self):pass
    class Future:
        def result(self,**kw):return None
        def cancel(self):pass
    pb=N(**{f'WorkflowService{method}Request':lambda **kw:N(**kw) for method in ['LoadJobTemplate','RunJob','StopJob','IsJobTemplateLoaded','UnloadJobTemplate']})
    workflow=N(RunJob=N(future=lambda *a,**k:Future()))
    s._service=lambda *a:(pb,workflow)
    methods=[]
    def rpc(stub,method,request,timeout=None):
        methods.append(method)
        return N(is_job_template_loaded=False)
    s._rpc=rpc
    s.stream_pb=N(ExperimentStreamingServiceMonitorAllExperimentsRequest=lambda **kw:N(**kw))
    s.stream=N(MonitorAllExperiments=lambda *a,**kw:Call())
    assert s.capture().sequence==7
    assert s.armed and 'UnloadJobTemplate' not in methods

def test_fine_focus_accepts_broad_peak_but_not_noise():
    rng=np.random.default_rng(8)
    z=np.linspace(-3,3,13)
    values=.0692-.00014*z*z+rng.normal(0,.00003,len(z))
    def samples(vals):return [dict(z_um=float(x),score=float(v),texture=.2) for x,v in zip(z,vals)]
    assert evaluate_fine_curve(samples(values)).success
    assert not evaluate_fine_curve(samples(.0692+rng.normal(0,.00012,len(z)))).success

def test_initial_autofocus_can_find_focus_far_from_start():
    s=SimulatedMicroscope();s.arm(Limits(0,2300,0,2300,60,150));s.move_z(125)
    result=autofocus_range(s,70,135,5,.5)
    assert result.success and abs(result.z_um-100)<=.6

def test_chip_inset_preserves_physical_edge_margin():
    p=inset_polygon([[0,0],[100,0],[100,100],[0,100]],10)
    assert np.allclose(p.min(axis=0),[10,10]);assert np.allclose(p.max(axis=0),[90,90])
    with pytest.raises(AcquisitionError):inset_polygon([[0,0],[10,0],[10,10],[0,10]],20)

def test_scan_resume_after_cancel_preserves_first_tile(tmp_path):
    s=SimulatedMicroscope();s.arm(Limits(-100,2400,-100,2400,60,150))
    t=CameraTransform([[1,0],[0,1]],(1,1),True)
    plan=ScanPlan([0,900,0,700],mode='survey',autofocus_every=0)
    def cb(event,*_):
        if event=='tile':s.stop()
    with pytest.raises(Cancelled):run_scan(s,plan,t,tmp_path,callback=cb)
    first=(tmp_path/'tiles/tile_000000.tiff').read_bytes()
    s.arm(s.limits)
    state=run_scan(s,plan,t,tmp_path,resume=True)
    assert state['state']=='complete'
    assert (tmp_path/'tiles/tile_000000.tiff').read_bytes()==first

def test_hardware_lease_rejects_second_writer_and_releases(tmp_path):
    from flakefinder.lease import HardwareLease
    one=HardwareLease('localhost:5002',tmp_path);two=HardwareLease('localhost:5002',tmp_path)
    try:
        one.acquire()
        with pytest.raises(SafetyError):two.acquire()
        one.close();two.acquire()
    finally:one.close();two.close()

def test_polygon_overlap_consolidates_repeats_but_not_neighbour():
    from flakefinder.scan import deduplicate
    base={'id':'a','chip_id':'A1','tile_id':'t1','touches_border':False,'area_um2':16,
          'contrast_rgb':[.1,.1,.1],'stage_xy_um':[10000,20000],
          'stage_polygon_um':[[9998,19998],[10002,19998],[10002,20002],[9998,20002]],'label':'unclassified'}
    repeat={**base,'id':'b','tile_id':'t2','stage_xy_um':[10000.6,20000],
            'stage_polygon_um':(np.array(base['stage_polygon_um'])+[.6,0]).tolist(),'contrast_rgb':[.14,.1,.1]}
    neighbour={**repeat,'id':'c','stage_xy_um':[10004.5,20000],
               'stage_polygon_um':(np.array(base['stage_polygon_um'])+[4.5,0]).tolist()}
    result=deduplicate([base,repeat,neighbour])
    assert len(result)==2
    assert len(result[0]['overlap_observations'])==1
    assert 'overlap_observations' not in base
