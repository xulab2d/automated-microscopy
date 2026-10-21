from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import pytest

from flakefinder.review_feedback import FeedbackConflict,ReviewFeedback,identity_digest


def identity():
    return {'run_id':'scan-20261015-example','observation':'chip001/tile_001700/511',
        'analysis_relative_path':'analysis/hbn/scan-20261015-example/chip001/version',
        'plan_sha256':'1'*64,'field_sha256':'2'*64,'source_tiff_sha256':'3'*64,
        'scan_request_sha256':'4'*64,'material':'hBN'}


def append(store,source=None,**overrides):
    args={'label':'thin_hbn','actor':'Reviewer','expected_revision':0,'client_event_id':'client-1',
        'source_region':{'polygon_source_px':[[10,12],[20,12],[15,22]],'area_um2':25.,'source_hashes':{'raw':'3'*64}}}
    args.update(overrides)
    return store.append(identity() if source is None else source,**args)


def state_path(root):return root/'annotations/flake-review/scan-20261015-example/current.json'


def test_durability_identity_digest_and_lightweight_snapshot(tmp_path):
    store=ReviewFeedback(tmp_path);source=identity();before=deepcopy(source)
    assert store.snapshot(source['run_id'])=={}
    assert not (tmp_path/'annotations').exists()
    result=append(store,source)
    assert result['revision']==1 and result['annotation']['label']=='thin_hbn'
    assert source==before
    key=identity_digest(source)
    assert key==ReviewFeedback.identity_digest(dict(reversed(list(source.items()))))
    snapshot=ReviewFeedback(tmp_path).snapshot(source['run_id'])
    assert snapshot[key]['revision']==1
    assert 'source_region' not in snapshot[key]
    snapshot[key]['label']='not_flake'
    assert store.snapshot(source['run_id'])[key]['label']=='thin_hbn'
    state=json.loads(state_path(tmp_path).read_text())
    assert state['events'][0]['source_region']['polygon_source_px']==[[10,12],[20,12],[15,22]]


def test_undo_increments_revision_keeps_audit_and_supports_relabel(tmp_path):
    store=ReviewFeedback(tmp_path);append(store)
    first=json.loads(state_path(tmp_path).read_text())['events'][0]
    undone=append(store,label=None,expected_revision=1,client_event_id='clear-2')
    assert undone['revision']==2 and undone['annotation'] is None
    saved=ReviewFeedback(tmp_path).snapshot(identity()['run_id'])[identity_digest(identity())]
    assert saved['label'] is None and saved['revision']==2
    state=json.loads(state_path(tmp_path).read_text())
    assert state['events'][0]==first and state['events'][1]['operation']=='clear'
    result=append(store,label='encapsulation_hbn',expected_revision=2,client_event_id='label-3')
    assert result['revision']==3


def test_idempotency_requires_entire_identical_request_and_precedes_revision_check(tmp_path):
    store=ReviewFeedback(tmp_path);first=append(store)
    repeat=append(ReviewFeedback(tmp_path))
    assert repeat['event_id']==first['event_id'] and repeat['idempotent']
    assert len(json.loads(state_path(tmp_path).read_text())['events'])==1
    for change in ({'label':'bulk'},{'actor':'Someone else'},{'expected_revision':1},{'source_region':{'polygon_source_px':[]}}):
        with pytest.raises(FeedbackConflict):append(store,**change)
    with pytest.raises(FeedbackConflict):append(store,expected_revision=0,client_event_id='different')


def test_distinct_source_generations_have_independent_revisions(tmp_path):
    store=ReviewFeedback(tmp_path);append(store)
    second=identity();second['field_sha256']='5'*64
    result=append(store,second,label='good_flake',client_event_id='other-source')
    assert result['revision']==1
    assert len(store.snapshot(second['run_id']))==2


@pytest.mark.parametrize('run',['..','../other','C:drive','a/b','a\\b',''])
def test_run_traversal_rejected(tmp_path,run):
    store=ReviewFeedback(tmp_path)
    with pytest.raises(ValueError):store.snapshot(run)
    source=identity();source['run_id']=run
    with pytest.raises(ValueError):append(store,source)
    assert not (tmp_path/'annotations').exists()


@pytest.mark.parametrize('path',['analysis/hbn/scan-other/version','../analysis/scan-20261015-example',
    '/analysis/scan-20261015-example','analysis/scan-20261015-example/../elsewhere',
    'C:/analysis/scan-20261015-example','analysis\\scan-20261015-example',
    'data/scan-20261015-example/tiles','data/scan-other/analysis-hbn-incremental'])
def test_wrong_run_or_path_escape_rejected(tmp_path,path):
    source=identity();source['analysis_relative_path']=path
    with pytest.raises(ValueError):append(ReviewFeedback(tmp_path),source)


def test_optional_legacy_hashes_and_exact_incremental_path_supported(tmp_path):
    source=identity();source.update(plan_sha256=None,source_tiff_sha256=None,
        analysis_relative_path='data/scan-20261015-example/analysis-hbn-incremental')
    assert append(ReviewFeedback(tmp_path),source)['revision']==1
    source['scan_request_sha256']=None
    with pytest.raises(ValueError):append(ReviewFeedback(tmp_path),source)


def test_atomic_publication_failure_leaves_old_labels_and_retry_possible(tmp_path,monkeypatch):
    import flakefinder.review_feedback as module
    store=ReviewFeedback(tmp_path);append(store);old=state_path(tmp_path).read_bytes()
    actual=module.os.replace
    def fail(*args):raise OSError('simulated interruption before atomic publication')
    monkeypatch.setattr(module.os,'replace',fail)
    with pytest.raises(OSError):append(store,label=None,expected_revision=1,client_event_id='undo')
    assert state_path(tmp_path).read_bytes()==old
    assert store.snapshot(identity()['run_id'])[identity_digest(identity())]['label']=='thin_hbn'
    monkeypatch.setattr(module.os,'replace',actual)
    assert append(store,label=None,expected_revision=1,client_event_id='undo')['revision']==2


def test_corrupt_projection_is_not_silently_reset(tmp_path):
    store=ReviewFeedback(tmp_path);append(store);path=state_path(tmp_path)
    state=json.loads(path.read_text());state['current']={};path.write_text(json.dumps(state))
    corrupt=path.read_bytes()
    with pytest.raises(ValueError,match='projection'):store.snapshot(identity()['run_id'])
    with pytest.raises(ValueError):append(store,expected_revision=1,client_event_id='later')
    assert path.read_bytes()==corrupt


def test_two_store_instances_serialize_competing_thread_edits(tmp_path):
    def attempt(event):
        try:return append(ReviewFeedback(tmp_path),client_event_id=event)['revision']
        except FeedbackConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['a','b']))
    assert sorted(str(r) for r in results)==['1','conflict']
    assert len(json.loads(state_path(tmp_path).read_text())['events'])==1
