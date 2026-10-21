import hashlib,json
from dataclasses import replace
from types import SimpleNamespace as N
import numpy as np
import pytest
from flakefinder.focus_checkpoint import find_local_focus,confirmation_ok
from flakefinder.model import Frame,Position,SafetyError
from flakefinder.imaging import save_frame


def fixture(tmp_path):
    root=tmp_path/'data';prior=root/'scan-20261011-010000-11111111';run=root/'scan-20261011-020000-22222222'
    folder=run/'discovery/candidate001';folder.mkdir(parents=True)
    base=prior/'discovery/candidate001/edge-focus/attempt_000';base.mkdir(parents=True)
    ref=Frame(np.ones((80,100,3),np.uint16),Position(10,20,30),(.5,.5),1,100,{})
    crop=Frame(np.ones((20,20,3),np.uint16),Position(11,21,31),(.5,.5),2,101,
        {'physical_stage_position':{'x':10.,'y':20.,'z':31.},'focus_mask_policy':'include_chip_edge','focus_only_roi_source_px':[40,30,60,50]})
    save_frame(base,'verified_focus',crop)
    (base/'focus-result.json').write_text(json.dumps({'success':True,'outcome':'resolved_peak','z_um':31,'score':2.}))
    paths=[base/x for x in ('focus-result.json','verified_focus.tiff','verified_focus.json')]
    contract={'prior_run_id':prior.name,'prior_files_sha256':{p.relative_to(prior).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    (run/'scan-request.json').write_text(json.dumps({'continuation':contract}))
    return folder,prior,base,ref,crop,contract


def test_same_xy_pinned_checkpoint_and_independent_confirmation(tmp_path):
    folder,prior,base,ref,crop,_=fixture(tmp_path)
    saved=find_local_focus(folder,{'prior_run_id':prior.name},[10,20],ref)
    assert saved['box']==[40,30,60,50] and saved['result']['z_um']==31
    assert confirmation_ok(saved,crop,{'texture':.1,'score':1.6},.25)
    assert not confirmation_ok(saved,crop,{'texture':.1,'score':1.4},.25)
    assert not confirmation_ok(saved,replace(crop,position=Position(11,21,32)),{'texture':.1,'score':2},.25)
    assert find_local_focus(folder,{'prior_run_id':prior.name},[11,20],ref) is None
    assert find_local_focus(folder,{},[10,20],ref) is None
    (base/'verified_focus.tiff').write_bytes(b'changed')
    with pytest.raises(SafetyError,match='evidence changed'):find_local_focus(folder,{'prior_run_id':prior.name},[10,20],ref)


def test_optics_and_parent_must_match(tmp_path):
    folder,prior,base,ref,crop,_=fixture(tmp_path)
    with pytest.raises(SafetyError,match='parent differs'):find_local_focus(folder,{'prior_run_id':'other'},[10,20],ref)
    changed=replace(ref,metadata={'objective_position':6})
    assert find_local_focus(folder,{'prior_run_id':prior.name},[10,20],changed) is None


def test_partial_bracket_is_only_a_search_seed_with_memory_lineage(tmp_path):
    folder,prior,base,ref,crop,contract=fixture(tmp_path)
    save_frame(base,'best_observed',crop)
    (base/'recovery.json').write_text(json.dumps({'state':'failed','error':'Memory recovery exhausted after 120.0s: headroom',
        'bracket_um':[29.,33.],'best_observed':{'z_um':31.,'score':2.},'samples':[{}]*20}))
    for n in ('recovery.json','best_observed.tiff','best_observed.json'):
        contract['prior_files_sha256'][(base/n).relative_to(prior).as_posix()]=hashlib.sha256((base/n).read_bytes()).hexdigest()
    contract['continuation_kind']='diagnosed_pre_capture_memory_recovery'
    (folder.parents[1]/'scan-request.json').write_text(json.dumps({'continuation':contract}))
    saved=find_local_focus(folder,{'prior_run_id':prior.name},[10,20],ref,partial=True)
    assert saved['partial'] and saved['prior_samples']==20 and 'success' not in saved['result']
    assert find_local_focus(folder,{'prior_run_id':prior.name},[10,21],ref,partial=True) is None
    contract['continuation_kind']='other'
    (folder.parents[1]/'scan-request.json').write_text(json.dumps({'continuation':contract}))
    assert find_local_focus(folder,{'prior_run_id':prior.name},[10,20],ref,partial=True) is None
