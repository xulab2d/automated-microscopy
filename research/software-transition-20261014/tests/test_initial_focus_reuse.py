import hashlib,json
import pytest
from flakefinder.initial_edge_focus import focus_saved_edge
from flakefinder.model import SafetyError
from flakefinder.initial_edge_focus import reusable_focus_request


def test_pinned_focus_follows_reused_parent_and_refuses_frame_or_bytes_change(tmp_path):
    origin='scan-20261011-000000-11111111';parent='scan-20261011-010000-22222222'
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))
    def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    first=tmp_path/'data'/origin;second=tmp_path/'data'/parent
    focus=first/'initial-edge-focus'
    write(focus/'focus-result.json',{'success':True,'z_um':15})
    write(focus/'focused-full-field.json',{})
    (focus/'focused-full-field.tiff').write_bytes(b'original image')
    write(first/'scan-request.json',{})
    write(first/'batch.json',{'coordinate_frame_id':'same'})
    inherited=reusable_focus_request(tmp_path,origin)
    request={'focus_first':inherited,'continuation':{'prior_run_id':origin,'prior_coordinate_frame_id':'same',
        'prior_files_sha256':{'scan-request.json':digest(first/'scan-request.json')}}}
    write(second/'scan-request.json',request)
    write(second/'worker-start.json',{'request_sha256':digest(second/'scan-request.json')})
    write(second/'batch.json',{'coordinate_frame_id':'same'})
    write(second/'initial-focus-reused.json',{'source_run':origin,'result_sha256':inherited['source_sha256']['focus-result.json']})
    derived=reusable_focus_request(tmp_path,parent)
    assert derived['reuse_verified_from']==parent and derived['source_run']==origin
    assert len(derived['reuse_lineage_sha256'])==4
    write(second/'batch.json',{'coordinate_frame_id':'changed'})
    with pytest.raises(SafetyError,match='frame changed'):reusable_focus_request(tmp_path,parent)
    write(second/'batch.json',{'coordinate_frame_id':'same'})
    (focus/'focused-full-field.tiff').write_bytes(b'changed image')
    with pytest.raises(SafetyError,match='pinned source evidence'):reusable_focus_request(tmp_path,parent)

def test_focus_reuse_rejects_unverified_parent_before_scope_access(tmp_path):
    run=tmp_path/'data/new';run.mkdir(parents=True)
    with pytest.raises(SafetyError,match='same-placement parent'):
        focus_saved_edge(None,{'context':{'prior_run_id':'parent'}},None,run,
            {'reuse_verified_from':'unrelated'},None)

def test_focus_reuse_checks_pinned_bytes_before_read_or_motion(tmp_path):
    run=tmp_path/'data/new';run.mkdir(parents=True)
    source=tmp_path/'data/parent/initial-edge-focus';source.mkdir(parents=True)
    for name in ('focus-result.json','focused-full-field.tiff','focused-full-field.json'):
        (source/name).write_bytes(b'original')
    request={'reuse_verified_from':'parent','source_sha256':{
        p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}}
    (source/'focused-full-field.tiff').write_bytes(b'changed')
    with pytest.raises(SafetyError,match='Pinned reused focus evidence changed'):
        focus_saved_edge(None,{'context':{'prior_run_id':'parent'}},None,run,request,None)
