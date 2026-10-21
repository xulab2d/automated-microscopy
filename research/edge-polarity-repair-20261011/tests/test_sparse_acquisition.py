"""Small deterministic image simulation; no microscope connection."""
import numpy as np,pytest
from flakefinder.sparse_acquisition import SparseEdgeOptions,discover_sparse_outline
from flakefinder.full_chip import _anchor_points
from test_chip_discovery import RECT
from sparse_profile_fixture import ImageStage,profile_for


def test_sparse_brackets_find_four_edges_with_budget_and_no_frontier(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    options=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    result=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference))
    assert result['state']=='estimated' and len(result['records'])<=17
    assert len(result['estimated_outline']['lines'])==4
    assert any(r['phase'].startswith('bisect_') for r in result['records'])
    assert not any('frontier' in r['phase'] for r in result['records'])
    assert result['all_chips_found'] is False
    before=scope.sequence
    replay=discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),resume=True)
    assert replay['state']=='estimated' and scope.sequence==before


def test_sparse_budget_and_spread_anchor_selection():
    with pytest.raises(ValueError,match='32'):SparseEdgeOptions([0,100,0,100],max_probes=33)
    points=np.array([[0,0],[20,0],[30,5],[0,1000],[1000,0],[980,0],[1000,1000],[980,990]])
    selected=_anchor_points(points,4)
    distances=np.linalg.norm(selected[:,None,:]-selected[None,:,:],axis=2)+np.eye(4)*10000
    assert distances.min()>500


def test_corner_seed_uses_local_support_not_whole_field_fraction(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    options=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    result=discover_sparse_outline(scope,[220,220],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference))
    first=result['records'][0]
    assert first['fractions']['substrate']<.55 and first['classification']=='unknown'
    assert result['seed_verification']['classification']=='substrate_seed'
    assert result['seed_verification']['patch_substrate_fraction']>=.9
    assert result['rays'] and not scope.stopped


def test_unverified_seed_is_unresolved_and_does_not_stop_hardware(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    result=discover_sparse_outline(scope,[100,100],scope.transform,reference,tmp_path,
        SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20),edge_validation_profile=profile_for(reference))
    assert result['state']=='unresolved' and result['reason']=='seed_not_verified'
    assert result['estimated_outline']['status']=='unresolved'
    assert len(result['records'])==1 and not result['rays']
    assert not scope.stopped and scope.armed


def test_actual_capture_failure_still_aborts_and_stops(tmp_path,monkeypatch):
    from flakefinder.model import AcquisitionError
    scope=ImageStage([RECT]);reference=scope.capture()
    def fail():raise AcquisitionError('camera transport failure')
    monkeypatch.setattr(scope,'capture',fail)
    with pytest.raises(AcquisitionError,match='camera transport failure'):
        discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,
            SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20),edge_validation_profile=profile_for(reference))
    assert scope.stopped
