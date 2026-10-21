import json
import os
from pathlib import Path

from flakefinder.live_progress import build_live_progress


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_saved_surface_focus_overrides_boundary_and_preserves_capture_time(tmp_path):
    write(tmp_path, 'batch.json', {'state':'running','phase':'focus_surface','current_chip':'chip002',
        'chip_states':{'chip002':{'discovery_folder':'discovery/candidate002'}},
        'boundary_attempts':[{'id':'candidate002','folder':'discovery/candidate002','state':'estimated'}],
        'search_bounds_um':[0,100,0,100]})
    write(tmp_path, 'discovery/candidate002/discovery.json', {'state':'estimated',
        'edge_focus_attempts':[{'state':'focused','result':{'samples':[{'z_um':1,'score':999}]}}],
        'estimated_outline':{'polygon_um':[[0,0],[50,0],[0,50]],'status':'estimated'}})
    write(tmp_path,'chip002/coverage-plan.json',{'tile_centres_um':[[2,3],[4,5]],
        'camera_shape':[10,20,3],'transform':{'matrix':[[.2,0],[0,.2]]}})
    write(tmp_path,'chip002/focus-surface/focus-surface.json', {'state':'preparing',
        'attempt_folder':'attempt-a','anchors':[{'index':1}], 'verified_points_um':[[1,2,3]]})
    write(tmp_path,'chip002/focus-surface/attempt-a/anchor_001/local/focus-curve.json',
        {'samples':[{'z_um':12,'score':.04,'phase':'coarse'},{'z_um':13,'score':.06,'phase':'fine'}]})
    path = write(tmp_path,'chip002/focus-surface/attempt-a/anchor_001/selection/full-field.json',
        {'position':{'x':4,'y':5,'z':12},'acquired_at':123,
         'metadata':{'capture_storage_guard':{'free_bytes_before_capture':456,'reserve_bytes':100}}})
    path.with_suffix('.jpg').write_bytes(b'preview')
    result = build_live_progress(tmp_path)
    assert result['focus']['source'] == 'chip_focus_surface'
    assert result['focus']['best_z_um'] == 13
    assert result['focus']['best_is_verified'] is False
    assert result['counts']['planned_tiles'] == 2
    assert result['stage_map']['current_position_at'] == 123
    assert result['storage']['observed_at'] == 123
    assert result['storage']['source'] == 'saved_pre_capture_storage_guard'
    assert result['current_image']['relative_path'].startswith('chip002/')

    # Plane-seeded checks save a different curve name until fallback begins.
    curve = tmp_path/'chip002/focus-surface/attempt-a/anchor_001/local/focus-curve.json'
    curve.unlink()
    write(tmp_path, 'chip002/focus-surface/attempt-a/anchor_001/local/quick-focus.json',
          {'quick_samples':[{'z_um':14,'score':.07,'phase':'quick'}]})
    result = build_live_progress(tmp_path)
    assert result['focus']['samples'] == [{'z_um':14,'score':.07,'phase':'quick'}]
    write(tmp_path, 'chip002/focus-surface/attempt-a/anchor_001/local/quick-focus.json',
          {'state':'focused', 'quick_samples':[{'z_um':14,'score':.07,'phase':'quick'}],
           'result':{'success':True,'samples':[{'z_um':15,'score':.09,'phase':'fine'}]}})
    result = build_live_progress(tmp_path)
    assert result['focus']['samples'] == [{'z_um':15,'score':.09,'phase':'fine'}]


def test_partial_missing_and_external_paths_are_read_only(tmp_path):
    write(tmp_path,'batch.json',{'phase':'boundary','boundary_attempts':[{'folder':'../outside'}]})
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = build_live_progress(tmp_path)
    assert result['current_image'] is None
    assert result['counts']['edge_probes'] == 0
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    (tmp_path/'batch.json').write_text('{"phase":')
    result = build_live_progress(tmp_path)
    assert result['available'] is False
    assert result['warnings']


def test_large_geometry_is_sampled_counts_and_deadline_retained(tmp_path):
    write(tmp_path,'batch.json',{'phase':'acquiring','chip_states':{'chip001':{}},
        'current_chip':'chip001','seed_records':[{'position_um':[i,i,1],'classification':'holder'} for i in range(10000)]})
    write(tmp_path,'chip001/coverage-plan.json',{'tile_centres_um':[[i,i] for i in range(5000)]})
    write(tmp_path,'chip001/manifest.json',{'completed':{'tile_%06d'%i:{'position':{'x':i,'y':i,'z':1},'sequence':i} for i in range(5000)}})
    write(tmp_path,'deadline-guard.json',{'state':'guarding','deadline_utc':'2026-10-09T16:00:00+00:00'})
    result = build_live_progress(tmp_path)
    assert result['counts']['seeds'] == 10000
    assert result['counts']['acquired_tiles'] == 5000
    assert result['counts']['seeds_sampled']
    assert result['timing']['deadline_at'] == 1791561600
    assert len(json.dumps(result)) < 300000
    assert result['stage_map']['seeds'][-1]['x'] == 9999


def test_raster_phase_and_latest_image_when_camera_counter_resets(tmp_path):
    write(tmp_path, 'batch.json', {'state':'running', 'phase':'tile',
        'current_chip':'chip002', 'chip_states':{'chip002':{'state':'acquiring'}}})
    completed = {}
    for i in range(2):
        name = 'tile_%06d' % i
        completed[name] = {'position':{'x':i, 'y':2, 'z':3}, 'sequence':0}
        p = write(tmp_path, 'chip002/tiles/'+name+'.json',
            {'position':completed[name]['position'], 'acquired_at':100+i})
        p.with_suffix('.jpg').write_bytes(b'preview')
        os.utime(p.with_suffix('.jpg'), (100+i, 100+i))
    write(tmp_path, 'chip002/manifest.json', {'completed':completed})
    result = build_live_progress(tmp_path)
    assert result['current_step'] == 'raster'
    assert result['current_image']['relative_path'] == 'chip002/tiles/tile_000001.jpg'
    assert result['stage_map']['current_position_um']['x'] == 1


def test_autonomous_reference_before_batch_shows_saved_focus_and_preview(tmp_path):
    write(tmp_path, 'autonomous-reference/proof.json', {'state':'edge_focus', 'bounds_um':[0,50000,0,50000],
        'observations':[{'raw_path':'autonomous-reference/search-000.tiff'}]})
    write(tmp_path, 'autonomous-reference/focus/recovery.json', {'state':'searching',
        'samples':[{'z_um':12,'score':.03,'phase':'direction_probe'}, {'z_um':14,'score':.08,'phase':'exponential_bracket'}]})
    for name, at in [('search-000',100), ('focus/best_observed',110)]:
        path = write(tmp_path, 'autonomous-reference/'+name+'.json',
            {'position':{'x':1000,'y':2000,'z':14}, 'acquired_at':at})
        path.with_suffix('.jpg').write_bytes(b'preview')
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = build_live_progress(tmp_path)
    assert result['available'] is True
    assert result['state'] == 'edge_focus'
    assert result['current_step'] == 'focus'
    assert result['focus']['source'] == 'autonomous_reference_focus'
    assert result['focus']['sample_count'] == 2
    assert result['focus']['best_z_um'] == 14
    assert result['focus']['best_is_verified'] is False
    assert result['current_image']['relative_path'] == 'autonomous-reference/focus/best_observed.jpg'
    assert result['stage_map']['current_position_at'] == 110
    assert result['counts']['acquired_tiles'] == 0
    assert before == {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_bootstrap_capture_before_proof_checkpoint_and_external_path_ignored(tmp_path):
    path = write(tmp_path, 'autonomous-reference/search-000.json', {'acquired_at':1,'position':{'x':1,'y':2,'z':3}})
    path.with_suffix('.jpg').write_bytes(b'preview')
    result = build_live_progress(tmp_path)
    assert result['available']
    assert result['current_image']['relative_path'] == 'autonomous-reference/search-000.jpg'
    assert result['focus']['status'] == 'unavailable'
    write(tmp_path, 'autonomous-reference/proof.json', {'state':'failed', 'observations':[{'raw_path':'../external.tiff'}]})
    result = build_live_progress(tmp_path)
    assert result['state'] == 'failed'
    assert result['current_image']['relative_path'] == 'autonomous-reference/search-000.jpg'


def test_bootstrap_does_not_override_campaign_focus(tmp_path):
    write(tmp_path, 'autonomous-reference/proof.json', {'state':'accepted'})
    write(tmp_path, 'autonomous-reference/focus/recovery.json', {'state':'focused','samples':[{'z_um':99,'score':100}]})
    write(tmp_path, 'batch.json', {'state':'running','phase':'boundary',
        'boundary_attempts':[{'folder':'discovery/candidate001'}]})
    write(tmp_path, 'discovery/candidate001/discovery.json', {'edge_focus_attempts':[
        {'state':'focusing','folder':'edge-focus/attempt_000'}]})
    write(tmp_path, 'discovery/candidate001/edge-focus/attempt_000/recovery.json',
        {'samples':[{'z_um':12,'score':.03}]})
    result = build_live_progress(tmp_path)
    assert result['focus']['source'] == 'boundary_edge_focus'
    assert result['focus']['best_z_um'] == 12


def test_bootstrap_map_records_search_and_bracketing_without_fake_raster_or_outline(tmp_path):
    proof=write(tmp_path,'autonomous-reference/proof.json',{'state':'searching','bounds_um':[0,100,0,100],
        'observations':[
            {'raw_path':'autonomous-reference/search-000.tiff','position_um':{'x':10,'y':20,'z':5}},
            {'raw_path':str(tmp_path/'autonomous-reference/bracket-001-00.tiff'),'position_um':[30,40,5]},
            {'raw_path':'../external.tiff','position_um':[900,900,5]}]})
    # Same frame in the preview set must not be counted twice. A new preview
    # without its proof checkpoint is still a saved capture, never a route point.
    for name,position in [('search-000',[10,20,5]),('bracket-001-01',[35,45,5])]:
        path=write(tmp_path,'autonomous-reference/'+name+'.json',{'position':position,'acquired_at':10})
        path.with_suffix('.jpg').write_bytes(b'no image decoding needed')
    write(tmp_path,'autonomous-reference/search-plan.json',{'route':[[i,i] for i in range(100)]})
    before={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result=build_live_progress(tmp_path)
    assert result['phase_label']=='Preparing focus and substrate reference'
    assert result['phase_group']=='reference_preparation'
    points=result['stage_map']['preparation_fields']
    assert {(p['x'],p['y']) for p in points}=={(10,20),(30,40),(35,45)}
    assert result['counts']['preparation_fields']==3
    assert result['counts']['preparation_search_fields']==1 and result['counts']['preparation_bracket_fields']==2
    assert result['counts']['seeds']==result['counts']['edge_probes']==result['counts']['acquired_tiles']==0
    assert result['stage_map']['outline_um']==[] and result['stage_map']['planned_tiles']==[]
    assert all(p['position_kind']=='saved_capture' and p['phase']=='reference_preparation' for p in points)
    assert before=={p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    # Preparation evidence remains on the map once campaign discovery starts;
    # it does not replace the new campaign phase or inflate its search count.
    write(tmp_path,'batch.json',{'state':'running','phase':'measuring_chip_edges','seed_records':[
        {'position_um':[60,70,5],'classification':'substrate_seed'}]})
    result=build_live_progress(tmp_path)
    assert result['phase_group']=='campaign' and result['phase']=='measuring_chip_edges'
    assert result['counts']['preparation_fields']==2 and result['counts']['seeds']==1
    assert result['counts']['acquired_tiles']==0


def test_large_bootstrap_map_samples_positions_but_keeps_observed_count(tmp_path):
    write(tmp_path,'autonomous-reference/proof.json',{'state':'failed','observations':[
        {'raw_path':f'autonomous-reference/search-{i:06d}.tiff','position_um':[i,i,1]} for i in range(3000)]})
    result=build_live_progress(tmp_path)
    assert result['counts']['preparation_fields']==3000 and result['counts']['preparation_fields_sampled']
    assert len(result['stage_map']['preparation_fields'])<=1000
    assert result['stage_map']['preparation_fields'][-1]['x']==2999
    assert result['counts']['acquired_tiles']==0 and len(json.dumps(result))<300000
