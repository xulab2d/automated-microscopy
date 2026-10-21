"""Completed rasters stay in their original owner and never become writable."""
import json,shutil
from pathlib import Path
import pytest
from flakefinder import completed_artifacts as artifacts,chip_campaign as campaign
from flakefinder.model import SafetyError,atomic_json
from test_chip_campaign_continuation import saved_campaign
from test_boundary_replay_recovery import recovery_case


@pytest.fixture
def referenced(recovery_case,tmp_path,monkeypatch):
    original,folder,state,plan,manifest,row,transform=recovery_case
    data=tmp_path/'data';data.mkdir();donor=data/'scan-20261013-221727-131d9ced';original.rename(donor)
    folder=donor/'chip001';manifest.update(state='complete_with_boundary_limits',tile_count=1)
    atomic_json(folder/'manifest.json',manifest)
    monkeypatch.setattr('flakefinder.scan_continuation.process_alive_strict',lambda pid:False)
    atomic_json(donor/'scan-status.json',{'id':donor.name,'pid':123,'hardware_closed':True,'state':'failed',
        'request_sha256':artifacts.file_hash(donor/'scan-request.json')})
    atomic_json(donor/'worker-start.json',{'id':donor.name,'pid':123,
        'request_sha256':artifacts.file_hash(donor/'scan-request.json')})
    mapping=artifacts.build_completed_chip_sources(donor,state)
    out=data/'scan-20261014-120000-12345678';(out/'plans').mkdir(parents=True)
    shutil.copy2(donor/'plans/chip001.json',out/'plans/chip001.json')
    atomic_json(out/'scan-request.json',{'rectangle_handoff':{'completed_chip_sources':mapping}})
    state['completed_chip_sources']=mapping
    return donor,out,state,manifest,transform


def test_completed_reference_replays_original_labels_without_copy_or_mutation(referenced,monkeypatch):
    donor,out,state,manifest,transform=referenced
    before=(donor/'chip001/manifest.json').read_bytes()
    original_hash=artifacts.file_hash
    def small_only(path):
        assert Path(path).suffix!='.tiff','Reference pinning reread a raw TIFF'
        return original_hash(path)
    monkeypatch.setattr(artifacts,'file_hash',small_only)
    assert artifacts.resolve_completed_chip(out,state,'chip001')==donor/'chip001'
    _,_,_,audit=campaign.restore_campaign_coverage(out,state,transform)
    assert audit[0]['registered_fields_verified']==1
    assert audit[0]['completed_chip_source']['source_run_id']==donor.name
    assert not (out/'chip001').exists() and (donor/'chip001/manifest.json').read_bytes()==before


def test_missing_labels_recover_from_original_source_release_and_raw(referenced):
    donor,out,state,manifest,transform=referenced
    (donor/'chip001/boundary-labels/tile_000000.npz').unlink()
    _,_,_,audit=campaign.restore_campaign_coverage(out,state,transform)
    recovery=audit[0]['missing_artifact_replay']
    assert recovery['aggregate_coverage_exact_match']
    assert recovery['replay_recoveries'][0]['original_acquisition_source_sha256']==manifest['algorithm_source_sha256']
    assert not (out/'chip001').exists()


@pytest.mark.parametrize('change',['manifest','coverage','source_request','source_status','local_plan','mapping','current'])
def test_reference_refuses_changed_identity_or_attempt_to_write(referenced,change):
    donor,out,state,manifest,transform=referenced
    if change=='manifest':atomic_json(donor/'chip001/manifest.json',{**manifest,'state':'stopped'})
    elif change=='coverage':(donor/'chip001/coverage-evidence.npz').write_bytes(b'changed')
    elif change=='source_request':atomic_json(donor/'scan-request.json',{})
    elif change=='source_status':atomic_json(donor/'scan-status.json',{'hardware_closed':False})
    elif change=='local_plan':atomic_json(out/'plans/chip001.json',{})
    elif change=='mapping':state['completed_chip_sources']['chip001']['tile_count']=2
    else:state['current_chip']='chip001'
    with pytest.raises(SafetyError):artifacts.resolve_completed_chip(out,state,'chip001')


def test_future_handoff_preserves_original_source_and_excludes_current_chip(referenced):
    donor,out,state,manifest,transform=referenced
    assert artifacts.build_completed_chip_sources(out,state)==state['completed_chip_sources']
    state['current_chip']='chip001'
    assert artifacts.build_completed_chip_sources(out,state)=={}


def test_no_mapping_preserves_ordinary_local_chip_path(tmp_path):
    assert artifacts.resolve_completed_chip(tmp_path,{},'chip002')==tmp_path/'chip002'


def test_referenced_owner_must_remain_closed_and_absent(referenced,monkeypatch):
    donor,out,state,manifest,transform=referenced
    monkeypatch.setattr('flakefinder.scan_continuation.process_alive_strict',lambda pid:True)
    with pytest.raises(SafetyError,match='owner'):artifacts.resolve_completed_chip(out,state,'chip001')


def test_transient_source_read_retries_from_unchanged_small_evidence(referenced,monkeypatch):
    import errno
    donor,out,state,manifest,transform=referenced;original=artifacts.read;calls=[]
    def read(path):
        if Path(path)==donor/'chip001/manifest.json':
            calls.append(path)
            if len(calls)==1:raise OSError(errno.EINVAL,'transient provider read')
        return original(path)
    monkeypatch.setattr(artifacts,'read',read)
    assert artifacts.resolve_completed_chip(out,state,'chip001')==donor/'chip001'
    assert len(calls)==2
