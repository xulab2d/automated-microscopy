"""Synthetic frames/files only; no microscope or real host lease acquisition."""
import json,hashlib
from types import SimpleNamespace as N
import numpy as np
import pytest
from flakefinder import acquisition_resources as resources
from flakefinder import chip_discovery,scan,full_chip,zen
from flakefinder.model import AcquisitionError,SafetyError,FocusSupportError,Frame,Position,CameraTransform
from flakefinder.autofocus import FocusResult
from flakefinder.lease import HardwareLease
from test_full_chip import planned,fast_focus,resign
from test_capture_overlap import job


def test_huge_search_and_raster_refused_before_coordinate_allocation(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Oversized allocation attempted')
    monkeypatch.setattr(np,'linspace',forbidden)
    with pytest.raises(ValueError,match='probe budget'):
        chip_discovery.plan_unknown_count_search([0,1e8,0,1e8],40.000001,40.,100)
    frame=Frame(np.ones((16,16,3),np.uint16),Position(0,0,1),(1.,1.),1)
    transform=CameraTransform([[1.,0.],[0.,1.]],(1.,1.),True)
    with pytest.raises(SafetyError,match='planning budget'):
        scan.tile_centres(scan.ScanPlan([0,1e8,0,1e8],max_tiles=100),frame,transform)


def test_low_ram_prevents_job_and_mosaic_allocation(job,monkeypatch):
    scope,events=job
    monkeypatch.setattr(resources.memory_capacity,'snapshot',lambda:dict(physical_available_bytes=1024**3,
        commit_available_bytes=0,address_available_bytes=1024**4))
    # Simulate the bounded wait with virtual time; no vendor job may be started.
    clock=[0.]
    scope.capture_memory_check=lambda:resources.wait_for_memory_headroom(scope.check_cancel,
        lambda seconds:clock.__setitem__(0,clock[0]+seconds),clock=lambda:clock[0],max_wait_s=3)
    with pytest.raises(AcquisitionError,match='capacity'):
        scope._capture_job()
    assert clock[0]==3
    assert events==[] and not scope.job_output_dir.exists()
    monkeypatch.setattr(np,'zeros',lambda *a,**k:pytest.fail('Overview allocated under low RAM'))
    with pytest.raises(AcquisitionError,match='capacity'):
        scan.Mosaic([0,2000,0,2000])


def test_decoded_rgb_budget_checked_before_copy():
    size=N(width=10000,height=10000)
    pixel=N(size=size,start_position=N(x=0,y=0),pixel_type=1,raw_data=b'')
    with pytest.raises(AcquisitionError,match='128 MiB'):
        zen.decode_frame(N(pixel_data=pixel,frame_size=size))


def test_different_checkouts_and_loopback_aliases_share_one_host_lease(tmp_path):
    one=HardwareLease('localhost:5002',tmp_path/'one',global_directory=tmp_path/'host')
    two=HardwareLease('127.0.0.1:5002',tmp_path/'two',global_directory=tmp_path/'host')
    try:
        one.acquire()
        with pytest.raises(SafetyError,match='lease unavailable'):two.acquire()
        one.close();two.acquire()
    finally:one.close();two.close()


def test_old_checkout_lock_blocks_new_host_lease_and_failure_releases_partial(tmp_path):
    import msvcrt
    legacy=tmp_path/'original/data/locks';legacy.mkdir(parents=True)
    (tmp_path/'original/flakefinder').mkdir();(tmp_path/'original/flakefinder/zen.py').write_text('# fixture')
    path=legacy/(hashlib.sha256(b'localhost:5002').hexdigest()[:16]+'.lock')
    old=path.open('a+b');old.write(b'0');old.flush();old.seek(0);msvcrt.locking(old.fileno(),msvcrt.LK_NBLCK,1)
    new=HardwareLease('localhost:5002',tmp_path/'original/research/staged/data/locks',global_directory=tmp_path/'host')
    independent=HardwareLease('localhost:5002',tmp_path/'other',global_directory=tmp_path/'host')
    try:
        with pytest.raises(SafetyError,match='lease unavailable'):new.acquire()
        assert new.file is None and new._files==[]
        independent.acquire();independent.close()  # Failed acquisition released host lock.
        old.close();new.acquire()
    finally:old.close();new.close();independent.close()


def test_unowned_stop_does_not_send_stop_to_another_client(job):
    scope,events=job
    import threading
    scope.cancel=threading.Event();scope.lease=N(file=None)
    assert 'does not own' in scope.stop()[0]
    assert events==[] and scope.cancel.is_set()


def force_local(monkeypatch):
    def reject(*args,**kwargs):raise AcquisitionError('Synthetic missing plane support')
    monkeypatch.setattr(full_chip,'bounded_plane_prediction',reject)
    monkeypatch.setattr(full_chip,'supported_roi',lambda frame,*args:{'box_px':[50,40,90,80],
        'point_xy_um':[frame.position.x,frame.position.y],'substrate_fraction':1.})


def test_local_focus_attempt_cap_is_durable_across_resume(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned;force_local(monkeypatch)
    plan['options']['max_local_focus_attempts']=1;resign(plan)
    with pytest.raises(FocusSupportError,match='budget exhausted'):
        full_chip.run_full_chip(scope,plan,reference,folder)
    before=json.loads((folder/'manifest.json').read_text())
    assert len(before['completed'])==1 and len(before['local_focus_attempts'])==1
    scope.stopped=False;scope.armed=True
    with pytest.raises(FocusSupportError,match='budget exhausted'):
        full_chip.run_full_chip(scope,plan,reference,folder,resume=True)
    after=json.loads((folder/'manifest.json').read_text())
    assert after['completed']==before['completed'] and after['local_focus_attempts']==before['local_focus_attempts']


def test_failed_local_focus_never_produces_a_final_tile(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned;force_local(monkeypatch)
    monkeypatch.setattr(full_chip,'autofocus',lambda adapter,*a,**k:FocusResult(False,100.,0.,0.,'No peak',[]))
    with pytest.raises(FocusSupportError,match='no final tile'):
        full_chip.run_full_chip(scope,plan,reference,folder)
    saved=json.loads((folder/'manifest.json').read_text())
    assert not saved['completed'] and saved['local_focus_attempts'][0]['state']=='unresolved'
    assert not list((folder/'tiles').glob('*.tiff')) and scope.stopped


def test_secondary_coverage_write_failure_preserves_focus_error_and_terminal_manifest(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned;force_local(monkeypatch)
    monkeypatch.setattr(full_chip,'autofocus',lambda adapter,*a,**k:FocusResult(False,100.,0.,0.,'No peak',[]))
    original=np.savez_compressed
    def fail_coverage(path,*a,**k):
        if str(path).endswith('coverage-evidence.npz'):raise OSError('Synthetic storage unavailable')
        return original(path,*a,**k)
    monkeypatch.setattr(np,'savez_compressed',fail_coverage)
    with pytest.raises(FocusSupportError,match='no final tile'):
        full_chip.run_full_chip(scope,plan,reference,folder)
    saved=json.loads((folder/'manifest.json').read_text())
    assert saved['state']=='failed' and saved['error'].startswith('Local focus unresolved')
    assert saved['derived_outputs_pending'] and 'Synthetic storage unavailable' in saved['finalization_errors'][0]
