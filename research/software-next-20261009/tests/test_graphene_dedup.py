from copy import deepcopy

import numpy as np
import pytest

from flakefinder.graphene_dedup import DedupOptions, _canonical, deduplicate_observations


TRANSFORM={'matrix':[[1.,0.],[0.,1.]],'calibrated':True}


def row(tile, *, shift=0., position=(0.,0.), label=1, color=(.1,.15,.12), covariance=.0001):
    polygon=[[shift,0.],[shift+10,0.],[shift+10,10.],[shift,10.]]
    pixel=(np.asarray(polygon)-position+99.5).tolist()
    return {'id':f'chip001/{tile}/{label}','field':f'chip001/{tile}','source_shape_hw':[200,200],
        'source_position_um':{'x':position[0],'y':position[1]},'polygon_source_px':pixel,'holes_source_px':[],
        'source_transform_sha256':_canonical(TRANSFORM),'source_tiff_sha256':'a'*64,
        'contrast_rgb':list(color),'contrast_covariance':(np.eye(3)*covariance).tolist(),
        'area_um2':100.,'material_pixel_fraction':.9,'measurement':{'interior_measurement_available':True},
        'stage_xy_um':[999999,999999]}


def run(rows, **kwargs):
    return deduplicate_observations(rows,run_id='run',chip_id='chip001',coordinate_frame_id='frame',
        transform=kwargs.pop('transform',TRANSFORM),**kwargs)


def test_registered_source_polygons_group_not_centroids_and_keep_every_original():
    a=row('tile_000001');b=row('tile_000002',position=(20.,0.));before=deepcopy([a,b])
    value=run([a,b])
    assert value['group_count']==1 and value['merged_observation_count']==1
    assert value['observations']==before and [a,b]==before
    assert value['groups'][0]['pair_evidence'][0]['iou']==pytest.approx(1.)
    assert value['representatives'][0]['observation_count']==2


def test_nearby_disjoint_polygons_do_not_group_even_with_identical_centroids():
    value=run([row('tile_000001'),row('tile_000002',shift=11)])
    assert value['group_count']==2 and value['pair_diagnostics'][0]['reason']=='disjoint_stage_bounds'


def test_hole_excludes_contained_observation():
    a=row('tile_000001');b=row('tile_000002')
    a['holes_source_px']=[(np.array([[1.,1.],[9.,1.],[9.,9.],[1.,9.]])+99.5).tolist()]
    b['polygon_source_px']=(np.array([[2.,2.],[8.,2.],[8.,8.],[2.,8.]])+99.5).tolist()
    value=run([a,b]);assert value['group_count']==2
    assert value['pair_diagnostics'][0]['overlap_fraction']==0


@pytest.mark.parametrize('color,covariance', [((.2,.15,.12),1.),((.14,.15,.12),.000001)])
def test_color_requires_absolute_and_covariance_agreement(color,covariance):
    a=row('tile_000001',covariance=covariance);b=row('tile_000002',color=color,covariance=covariance)
    value=run([a,b]);assert value['group_count']==2
    assert value['pair_diagnostics'][0]['reason']=='color_disagreement'


def test_transitive_chain_never_merges_nonmatching_endpoints():
    rows=[row('tile_000001'),row('tile_000002',shift=2),row('tile_000003',shift=4)]
    value=run(rows)
    assert sorted(g['observation_count'] for g in value['groups'])==[1,2]
    assert all(p['accepted'] for g in value['groups'] for p in g['pair_evidence'])
    assert run(list(reversed(rows)))['groups']==value['groups']


def test_two_regions_from_same_field_never_share_a_group():
    value=run([row('tile_000001'),row('tile_000001',label=2),row('tile_000002')])
    assert sorted(g['observation_count'] for g in value['groups'])==[1,2]


@pytest.mark.parametrize('key,value',[('coordinate_frame_id','other'),('run_id','other'),('chip_id','other'),
    ('source_transform_sha256','b'*64),('source_tiff_sha256',None),('contrast_rgb',None)])
def test_context_or_provenance_failure_preserves_singleton(key,value):
    a=row('tile_000001');b=row('tile_000002');b[key]=value
    result=run([a,b]);assert result['group_count']==2 and b['id'] in result['ineligible_observations']
    assert result['observations']==[a,b]


def test_quality_representative_prefers_supported_interior_then_uncertainty():
    a=row('tile_000001',covariance=.001);b=row('tile_000002',covariance=.0001)
    assert run([a,b])['groups'][0]['representative_id']==b['id']
    b['measurement']['interior_measurement_available']=False
    assert run([a,b])['groups'][0]['representative_id']==a['id']


def test_rotated_camera_transform_is_applied_before_overlap():
    transform={'matrix':[[0.,-.5],[.5,0.]],'calibrated':True};a=row('tile_000001');b=row('tile_000002')
    b['source_position_um']['x']=10
    # xstage=-.5*ypixel, so shifting ypixel by20 offsets the stage centre shift.
    b['polygon_source_px']=(np.asarray(b['polygon_source_px'])+[0,20]).tolist()
    for item in [a,b]:item['source_transform_sha256']=_canonical(transform)
    assert run([a,b],transform=transform)['group_count']==1


def test_bounded_raster_rejects_unresolved_overlap_without_large_allocation():
    a=row('tile_000001');b=row('tile_000002')
    for item in (a,b):
        item['source_shape_hw']=[1000,1000]
        item['polygon_source_px']=[[100,100],[900,100],[900,900],[100,900]]
    result=run([a,b],options=DedupOptions(maximum_raster_pixels=64))
    assert result['group_count']==2 and result['pair_diagnostics'][0]['reason']=='overlap_resolution_capacity'


def test_malformed_covariance_or_geometry_does_not_delete_observations():
    a=row('tile_000001');b=row('tile_000002');a['contrast_covariance'][0][0]=-1
    b['polygon_source_px']=[[float('nan'),1],[1,2],[3,4]]
    result=run([a,b]);assert result['group_count']==2 and len(result['ineligible_observations'])==2


def test_requires_unique_ids_and_explicit_context_and_valid_options():
    a=row('tile_000001')
    with pytest.raises(ValueError,match='Unique'):run([a,a])
    with pytest.raises(ValueError,match='frame'):deduplicate_observations([a],run_id='r',chip_id='chip001',coordinate_frame_id='',transform=TRANSFORM)
    with pytest.raises(ValueError):DedupOptions(minimum_iou=2)
    assert run([])['groups']==[]


def test_unknown_classes_can_group_and_spatial_index_avoids_distant_pairs():
    a=row('tile_000001');b=row('tile_000002')
    a['class_index']=b['class_index']=-1
    a['selected_for_review']=b['selected_for_review']=False
    assert run([a,b])['group_count']==1
    rows=[]
    for n in range(1000):
        item=row(f'tile_{n:06d}');item['source_position_um']['x']=n*250.
        rows.append(item)
    result=run(rows)
    assert result['group_count']==1000 and result['pair_comparison_count']==0


def disk_script():
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('group_saved_graphene',Path(__file__).resolve().parents[1]/'scripts/group_saved_graphene.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_disk_groups_match_memory_complete_link_and_resume(tmp_path):
    script=disk_script();db=script.open_database(tmp_path/'index.sqlite')
    context={'run_id':'run','chip_id':'chip001','coordinate_frame_id':'frame'};options=DedupOptions()
    rows=[row('tile_000001'),row('tile_000002',shift=2),row('tile_000003',shift=4),row('tile_000004',shift=30)]
    for item in rows:script.store_field(db,item['field'],'a'*64,{'rows':[item]},context,TRANSFORM,options,25.)
    script.group_database(db,context,TRANSFORM,options)
    groups=script.export_groups(db,context,TRANSFORM,options)
    expected=[g for g in run(rows)['groups'] if g['observation_count']>1]
    assert [(g['id'],g['representative_id'],g['observation_ids']) for g in groups]==[(g['id'],g['representative_id'],g['observation_ids']) for g in expected]
    assert script.group_database(db,context,TRANSFORM,options)['pair_checks_this_invocation']==0
    assert db.execute('SELECT COUNT(*) FROM observations WHERE group_id IS NOT NULL').fetchone()[0]==4
    db.close()


def test_disk_index_is_field_atomic_and_preserves_unknowns_and_ineligible(tmp_path):
    script=disk_script();db=script.open_database(tmp_path/'index.sqlite')
    context={'run_id':'run','chip_id':'chip001','coordinate_frame_id':'frame'};options=DedupOptions()
    rows=[row('tile_000001',label=1),row('tile_000001',label=2)]
    calls=0
    def stop():
        nonlocal calls
        calls+=1
        if calls==2:raise InterruptedError('stop')
    with pytest.raises(InterruptedError):script.store_field(db,rows[0]['field'],'a'*64,{'rows':rows},context,TRANSFORM,options,25.,stop)
    assert db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0
    assert db.execute('SELECT COUNT(*) FROM fields').fetchone()[0]==0
    rows[0]['contrast_rgb']=None;rows[1]['class_index']=-1
    script.store_field(db,rows[0]['field'],'a'*64,{'rows':rows},context,TRANSFORM,options,25.)
    script.group_database(db,context,TRANSFORM,options)
    assert db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==2
    assert db.execute('SELECT COUNT(*) FROM groups').fetchone()[0]==2
    db.close()
