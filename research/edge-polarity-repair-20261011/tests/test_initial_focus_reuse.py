import hashlib,json
import pytest
from flakefinder.initial_edge_focus import focus_saved_edge
from flakefinder.model import SafetyError

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
