"""Pure work accounting and opt-in immutable saved-image replay; no hardware."""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from flakefinder.sparse_acquisition import SparseEdgeOptions,rectangle_work_budget,discover_sparse_outline


def options(**kw):
    return SparseEdgeOptions([0,18000,0,18000],rectangle_mode=True,**kw)


def test_work_is_derived_from_rays_and_actual_one_eight_image_focus_policy():
    budget=rectangle_work_budget(options(),[9000,9000],remaining_seconds=7200,field_um=[400,500])
    assert budget['max_elapsed_s']==7200
    assert budget['time_policy']=='remaining_user_campaign_deadline'
    assert len(budget['side_allowances'])==4
    assert all(side['capture_allowance']==6+7+3 for side in budget['side_allowances'])
    assert budget['size_remeasurement_capture_allowance']==8
    assert budget['geometry_capture_allowance']==1+4*16+19+1+8
    assert budget['max_total_captures']==budget['geometry_capture_allowance']+8
    assert budget['remaining_focus_attempts']==1
    assert rectangle_work_budget(options(max_probes=100),[9000,9000])['max_total_captures']>64
    with pytest.raises(ValueError):SparseEdgeOptions([0,18000,0,18000],max_probes=100)


def test_repeated_resume_keeps_focus_consumption_and_seed_relative_sides():
    saved={'records':[{'requested_xy_um':[9000,9000],
                      'edge':{'normal':[0,-1],'offset_um':-10000}}],
           'edge_focus_attempts':[{'state':'accepted'}],
           'operation_budget':{'captures_started':64,'elapsed_seconds':450},
           'rays':{'south':{'first_holder_distance_um':4500,'last_substrate_distance_um':4300}}}
    first=rectangle_work_budget(options(),[9000,9000],saved,remaining_seconds=7200)
    assert 'south' in first['missing_sides'] # Other chip's opposite-facing edge is outside seed.
    south=next(s for s in first['side_allowances'] if s['side']=='south')
    assert south['coarse_captures']==0 and south['bisection_captures']==1
    assert first['remaining_focus_attempts']==0 and first['previous_captures_started']==64
    for _ in range(3):
        saved['rectangle_probe_allowance']=first
        again=rectangle_work_budget(options(),[9000,9000],saved,remaining_seconds=7000)
        assert again['remaining_focus_attempts']==0
        assert again['max_total_captures']==first['max_total_captures']
        assert again['saved_focus_attempts']==1 and again['previous_captures_started']==64
    saved['records'][0]['edge']['offset_um']=-5000
    reduced=rectangle_work_budget(options(),[9000,9000],saved)
    assert 'south' not in reduced['missing_sides']
    assert reduced['max_total_captures']<first['max_total_captures']
    upgrade=rectangle_work_budget(options(),[9000,9000],saved,revalidate_saved=True)
    assert set(upgrade['missing_sides'])=={'east','north','west','south'}
    assert upgrade['saved_sides_pending_revalidation']
    assert upgrade['remaining_focus_attempts']==0
    assert upgrade['max_total_captures']>=first['max_total_captures']


def test_saved_lab_second_candidate_zero_capture_recovery(tmp_path):
    """Set FLAKEFINDER_RECTANGLE_SECOND_REPLAY to stopped run; donor stays unchanged."""
    from flakefinder.model import Limits,CameraTransform,Position,atomic_json
    from flakefinder.imaging import load_frame
    from flakefinder.chip_discovery import file_hash
    run=os.environ.get('FLAKEFINDER_RECTANGLE_SECOND_REPLAY')
    if not run:pytest.skip('No saved second-candidate replay requested')
    run=Path(run);source=run/'discovery'/'candidate002';path=source/'discovery.json'
    original_hash=file_hash(path);state=json.loads(path.read_text())
    source_hashes={str(source/k):v for r in state['records'] for k,v in r['hashes'].items()}
    assert all(file_hash(Path(k))==v for k,v in source_hashes.items())
    for record in state['records']:
        record['image']=str(source/record['image']);record['labels_file']=str(source/record['labels_file'])
        record['hashes']={str(source/k):v for k,v in record['hashes'].items()}
    atomic_json(tmp_path/'discovery.json',state)
    class NoHardware:
        armed=True;limits=Limits(**state['limits'])
        def check_cancel(self):pass
        def position(self):return Position(*state['records'][0]['position_um'])
        def __getattr__(self,name):
            if name in ('lease','focus_target_tolerance_um'):return None
            if name in ('capture','travel','move_z','stop'):raise AssertionError('Offline replay attempted hardware: '+name)
            raise AttributeError(name)
    result=discover_sparse_outline(NoHardware(),state['seed_xy_um'],CameraTransform(**state['transform']),
        load_frame(run/'calibration'/'substrate.tiff'),tmp_path,
        SparseEdgeOptions(**{**state['options'],'rectangle_mode':True}),
        context=state['context'],edge_validation_profile=state['edge_validation_profile'],
        focus_search_bounds_um=state.get('edge_focus_policy',{}).get('search_bounds_um'),max_elapsed_s=7200,
        resume=True,continue_unresolved=True,allow_source_change=True)
    outline=result['estimated_outline']
    assert result['state']=='estimated' and len(outline['lines'])==4
    assert result['operation_budget']['captures_started']==0 and result['rectangle_replay_closed']
    assert all(np.dot(line['normal'],state['seed_xy_um'])<line['offset_um'] for line in outline['lines'])
    assert min(outline['measured_dimensions_um'])<5000 # Approximate user prior is not a rejection threshold.
    assert any(key.endswith('probe_000022.tiff') and value.get('edge') for key,value in result['rectangle_reanalysis'].items())
    assert result['rectangle_probe_allowance']['remaining_focus_attempts']==0
    assert result['records']==state['records'] and file_hash(path)==original_hash
    assert all(file_hash(Path(k))==v for k,v in source_hashes.items())
    atomic_json(tmp_path/'saved-data-replay-report.json',{
        'donor_discovery':str(path),'donor_discovery_sha256':original_hash,'source_hashes':source_hashes,
        'new_captures':0,'new_motion':False,'estimated_outline':outline,
        'work_budget':result['rectangle_probe_allowance'],'reanalysis':result['rectangle_reanalysis']})
