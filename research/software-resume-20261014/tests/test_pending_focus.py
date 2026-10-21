from pathlib import Path
from types import SimpleNamespace as N
from dataclasses import asdict,replace
import json,hashlib
import numpy as np
import pytest
from flakefinder import pending_focus as p,autofocus as af,motion
from flakefinder.model import Frame,Position,SafetyError,Cancelled

def fixture(tmp_path):
    old=tmp_path/'data/scan-20261011-010000-11111111';new=tmp_path/'data/scan-20261011-020000-22222222'
    folder=old/'discovery/candidate002/edge-focus/attempt_000/partial-refinement';folder.mkdir(parents=True)
    (old/'observations').mkdir();(new/'discovery/candidate002').mkdir(parents=True)
    samples=[dict(z_um=z,score=5-(z-31)**2*.2,texture=.5,noise=.001,phase='quick') for z in (29,30,31,32,33)]
    result=af.fit_quick_focus_curve(samples);assert result.success
    record=dict(state='failed',error='Memory recovery exhausted after 120.0s: capacity',quick_samples=samples,fitted_quick_result=asdict(result))
    (folder/'quick-focus.json').write_text(json.dumps(record))
    for i,sample in enumerate(samples):
        obs=dict(kind='persisted_vendor_observation',observation_kind='quick_focus_sample',observation_context_folder=str(folder),
          capture=dict(job_completed=True,job_unloaded=True),measurement=sample,position=dict(x=10,y=20,z=sample['z_um']),pixel_size_um=[.5,.5],
          analysis_frame=dict(metadata=dict(focus_mask_policy='include_chip_edge',focus_only_roi_source_px=[10,10,30,30])))
        (old/f'observations/{i}.json').write_text(json.dumps(obs))
    files=p.pending_files(old);assert len(files)==6
    contract=dict(continuation_kind='diagnosed_pre_capture_memory_recovery',prior_run_id=old.name,
      prior_files_sha256={n:hashlib.sha256((old/n).read_bytes()).hexdigest() for n in files})
    (new/'scan-request.json').write_text(json.dumps(dict(continuation=contract)))
    ref=Frame(np.ones((50,50,3)),Position(10,20,31),(.5,.5),1)
    return old,new,ref,contract

def test_pins_position_roi_optics_and_recomputes_fit(tmp_path):
    old,new,ref,c=fixture(tmp_path);context=dict(prior_run_id=old.name)
    saved=p.find_pending(new/'discovery/candidate002',context,[10,20],ref)
    assert saved['fitted']['z_um']==pytest.approx(31) and len(saved['samples'])==5
    assert p.find_pending(new/'discovery/candidate002',context,[11,20],ref) is None
    assert p.find_pending(new/'discovery/candidate002',context,[10,20],replace(ref,metadata={'objective_position':6})) is None
    (old/'observations/0.json').write_text('{}')
    with pytest.raises(SafetyError,match='observation changed'):p.find_pending(new/'discovery/candidate002',context,[10,20],ref)

@pytest.mark.parametrize('score,success',[(5,True),(4,False)])
def test_missing_confirmation_is_exactly_one_new_image(tmp_path,monkeypatch,score,success):
    old,new,ref,c=fixture(tmp_path);saved=p.find_pending(new/'discovery/candidate002',dict(prior_run_id=old.name),[10,20],ref)
    frame=Frame(np.ones((20,20,3)),Position(10,20,31),(.5,.5),2,metadata={'physical_stage_position':dict(x=10,y=20,z=31),'focus_only_roi_source_px':[10,10,30,30]})
    calls=[];scope=N(check_cancel=lambda:None,focus_target_tolerance_um=.25)
    monkeypatch.setattr(motion,'move_z_bounded',lambda s,z:calls.append(('move',z)))
    def capture(*a):calls.append(('capture',));return frame,dict(score=score,texture=.5)
    monkeypatch.setattr(af,'capture_metric',capture);monkeypatch.setattr(af,'checkpoint_observation',lambda *a:None)
    result=p.confirm_pending(scope,saved,new/'discovery/candidate002/edge-focus/attempt_000/pending-confirmation')
    assert result.success==success and sum(x[0]=='capture' for x in calls)==1 and len(result.samples)==6

def test_operator_stop_prevents_confirmation_capture(tmp_path,monkeypatch):
    old,new,ref,c=fixture(tmp_path);saved=p.find_pending(new/'discovery/candidate002',dict(prior_run_id=old.name),[10,20],ref)
    def stop():raise Cancelled('operator STOP')
    monkeypatch.setattr(af,'capture_metric',lambda *a:pytest.fail('Capture after STOP'))
    with pytest.raises(Cancelled):p.confirm_pending(N(check_cancel=stop),saved,new/'discovery/candidate002/edge-focus/attempt_000/pending-confirmation')
