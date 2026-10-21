import copy
import json
import pytest

from flakefinder.suite_labels import ReviewEventStore, RevisionConflict, SourceIdentityError, digest, polygon


SOURCE = dict(dataset_id='dataset-01', field_key='chip001/tile_000139', source_sha256='a'*64, shape_hw=[100, 150], group_id='dataset-01/chip001')
OP = dict(kind='annotate', region_id='r1', labels={'semantic':'residue'})


def save(store, revision=0, operations=None, event='event-000001'):
    return store.append(SOURCE, expected_revision=revision, operations=operations or [copy.deepcopy(OP)], actor='Test reviewer', client_event_id=event, machine_region_ids=['r1'])


def test_durable_idempotent_undo_and_reconstruction(tmp_path):
    store = ReviewEventStore(tmp_path)
    first = save(store)
    assert first['revision'] == 1
    assert save(store)['head_sha256'] == first['head_sha256']
    assert ReviewEventStore(tmp_path).read(SOURCE)['regions']['r1']['labels']['semantic'] == 'residue'
    undone = save(store, 1, [dict(kind='undo', target_event_id=first['last_undoable_event_id'])], 'event-000002')
    assert undone['revision'] == 2 and undone['regions'] == {}
    third = save(store, 2, [dict(kind='annotate', region_id='r1', labels={'semantic':'uncertain'})], 'event-000003')
    assert third['regions']['r1']['labels']['semantic'] == 'uncertain'
    exported = store.export()['regions'][0]
    assert exported['source_identity'] == SOURCE and exported['group_id'] == SOURCE['group_id']


def test_source_revision_and_sync_fork_fail_closed(tmp_path):
    store = ReviewEventStore(tmp_path)
    save(store)
    with pytest.raises(RevisionConflict):
        save(store, event='event-new-001')
    with pytest.raises(SourceIdentityError):
        store.read({**SOURCE, 'source_sha256':'b'*64})
    original = next(store.folder(SOURCE).glob('*.json'))
    fork = json.loads(original.read_text())
    fork['event_id'] = 'fork'; fork.pop('event_sha256')
    fork['event_sha256'] = digest(fork)
    (original.parent / 'fork.json').write_text(json.dumps(fork))
    with pytest.raises(RevisionConflict):
        store.read(SOURCE)


def test_geometry_and_material_evidence_remain_independent(tmp_path):
    store = ReviewEventStore(tmp_path)
    p = [[5,5],[30,5],[30,30],[5,30]]
    first = save(store, operations=[dict(kind='create_region', region_id='human-child1', parent_ids=['r1'], polygons_px=[p])])
    assert first['regions']['human-child1']['parent_ids'] == ['r1']
    assert first['regions']['human-child1']['labels'] == {}
    with pytest.raises(ValueError, match='measurement'):
        save(store, 1, [dict(kind='annotate', region_id='r1', labels={'semantic':'flake_of_interest','material_status':'confirmed','evidence_method':'visual'})], 'event-000002')
    with pytest.raises(ValueError):
        polygon([[0,0],[1000,0],[0,5]], SOURCE['shape_hw'])
    with pytest.raises(ValueError):
        polygon([[0,0],[10,10],[0,10],[10,0]], SOURCE['shape_hw'])
    with pytest.raises(ValueError, match='Machine proposals'):
        save(store, 1, [dict(kind='set_hidden', region_id='r1', hidden=True)], 'event-000003')


def test_proposal_geometry_is_immutable_annotation_provenance(tmp_path):
    store = ReviewEventStore(tmp_path)
    proposal = dict(id='r1', proposal_fingerprint='b'*64, polygons_px=[[[5,5],[10,5],[10,10]]], mask_sha256='c'*64)
    save(store, operations=[{**OP, 'source_proposal':proposal}])
    result = store.export()['regions'][0]
    assert result['source_proposal'] == proposal
    assert result['human_geometry'] is None


def test_existing_writer_lock_preserves_pending_work(tmp_path):
    store = ReviewEventStore(tmp_path)
    folder = store.folder(SOURCE); folder.mkdir(parents=True)
    (folder / '.writer.lock').write_text('other process')
    with pytest.raises(RevisionConflict, match='writer'):
        save(store)
    assert (folder / '.writer.lock').read_text() == 'other process'
