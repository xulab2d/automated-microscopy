"""Full discovery over synthetic raw images with a bright internal flake."""
import numpy as np
import pytest

from sparse_profile_fixture import ImageStage,profile_for
from flakefinder.model import Position,Limits,Frame
from flakefinder.stage_background import KEY,build_model
from flakefinder.sparse_acquisition import discover_sparse_outline,SparseEdgeOptions


class FlakeStage(ImageStage):
    def capture(self):
        frame=super().capture()
        h,w=frame.rgb.shape[:2]; yy,xx=np.mgrid[:h,:w]
        x=xx-(w-1)/2+frame.position.x; y=yy-(h-1)/2+frame.position.y
        flake=(x>6300)&(x<6550)&(y>4700)&(y<5300)
        frame.rgb[flake]=[3000,3500,3300]
        return frame


def run_discovery(tmp_path,minimum,*,scope_type=FlakeStage,seed=(5000,5000),max_captures=None):
    scope=scope_type([[[2500,2500],[7500,2500],[7500,7500],[2500,7500]]],shape=(400,500))
    scope.p=Position(5000,5000,100)
    scope.limits=Limits(0,11000,0,11000,50,150,3000,10)
    reference=scope.capture()
    rgb=reference.rgb.copy();rgb[:,250:]=[400,400,400]
    control=Frame(rgb,reference.position,reference.pixel_size_um,reference.sequence,metadata=dict(reference.metadata))
    evidence={'edge':{'normal':[1.,0.],'offset_um':5000.},'raw_validation':{'accepted':True}}
    reference.metadata[KEY]=build_model(reference,control,evidence,scope.transform,
        {'accepted':True,'bright_control_rgb':[1800,1500,1950]},
        provenance={'edge_tiff_sha256':'a'*64,'edge_metadata_sha256':'b'*64})
    options=SparseEdgeOptions([20,10980,20,10980],rectangle_mode=True,cell_um=20,rectangle_min_chip_um=minimum)
    result=discover_sparse_outline(scope,list(seed),scope.transform,reference,tmp_path,options,
                                  edge_validation_profile=profile_for(reference),max_total_captures=max_captures)
    scope.reference_frame=reference;scope.edge_options=options
    return scope,result


def test_full_search_passes_internal_flake_and_finds_real_outer_edges(tmp_path):
    scope,result=run_discovery(tmp_path,5000.)
    assert result['state']=='estimated', result.get('reason')
    assert np.allclose(result['estimated_outline']['measured_dimensions_um'],[5000,5000],atol=30)
    false_field=next(r for r in result['records'] if r['requested_xy_um']==[6500.,5000.])
    assert false_field['edge'] is None
    assert false_field['fractions']['holder']==0
    assert max(p[0] for p in scope.moves)>7500
    assert not result.get('edge_focus_attempts')


def test_small_real_chip_is_remeasured_not_padded_or_rejected(tmp_path):
    _,result=run_discovery(tmp_path,7000.)
    assert result['state']=='estimated', result.get('reason')
    check=result['size_prior_check']
    assert check['state']=='confirmed_independently'
    assert len(check['checks'])==8 and all(row['accepted'] for row in check['checks'])
    assert np.allclose(result['estimated_outline']['measured_dimensions_um'],[5000,5000],atol=30)
    assert result['operation_budget']['captures_started']<=result['rectangle_probe_allowance']['max_total_captures']


def test_resume_retries_transient_cloud_file_open_without_recapturing(tmp_path,monkeypatch):
    from flakefinder import sparse_acquisition as sparse
    scope,result=run_discovery(tmp_path,5000.)
    original=sparse.file_hash;attempts=[];sequence=scope.sequence
    target=tmp_path/result['records'][0]['image']
    def transient(path):
        if path==target:
            attempts.append(True)
            if len(attempts)==1:raise FileNotFoundError(2,'Transient provider open',str(path))
        return original(path)
    monkeypatch.setattr(sparse,'file_hash',transient)
    resumed=discover_sparse_outline(scope,[5000,5000],scope.transform,scope.reference_frame,tmp_path,scope.edge_options,
        edge_validation_profile=profile_for(scope.reference_frame),resume=True,continue_unresolved=True,allow_source_change=True)
    assert resumed['state']=='estimated' and len(attempts)>=2
    assert scope.sequence==sequence


def test_ambiguous_size_confirmation_does_not_accept_original_outline(tmp_path):
    class OccludedConfirmation(FlakeStage):
        def capture(self):
            frame=super().capture()
            if (min(abs(frame.position.x-v) for v in [2500,7500])<10 and
                    min(abs(frame.position.y-v) for v in [4000,6000])<20):
                frame.rgb[:]=[3000,3500,3300]
            return frame
    _,result=run_discovery(tmp_path,7000.,scope_type=OccludedConfirmation)
    assert result['state']=='unresolved'
    assert result['reason']=='minimum_width_remeasurement_failed'
    assert result['estimated_outline']['polygon_um']==[]
    assert any(not row['accepted'] for row in result['size_prior_check']['checks'])


@pytest.mark.parametrize('seed',[(2800,7000),(7000,2800)])
def test_offcentre_seed_finds_same_chip(tmp_path,seed):
    _,result=run_discovery(tmp_path,5000.,seed=seed)
    assert result['state']=='estimated', result.get('reason')
    assert np.allclose(result['estimated_outline']['measured_dimensions_um'],[5000,5000],atol=30)


def test_interrupted_size_check_stays_unresolved_and_resume_finishes_checks(tmp_path):
    scope,result=run_discovery(tmp_path,7000.,max_captures=15)
    assert result['state']=='unresolved'
    assert result['reason']=='minimum_width_remeasurement_failed'
    assert result['estimated_outline']['polygon_um']==[]
    assert result['size_prior_check']['interrupted_reason']=='capture_budget_exhausted'
    before=scope.sequence
    resumed=discover_sparse_outline(scope,[5000,5000],scope.transform,scope.reference_frame,tmp_path,scope.edge_options,
        edge_validation_profile=profile_for(scope.reference_frame),resume=True,continue_unresolved=True)
    assert scope.sequence>before  # Four saved sides must not bypass the pending checks.
    assert resumed['state']=='estimated'
    assert resumed['size_prior_check']['state']=='confirmed_independently'


def test_rejected_saved_edge_cannot_reappear_on_same_source_resume(tmp_path):
    import json
    from flakefinder.imaging import load_frame
    from flakefinder.rectangle_edges import measure_rectangle_side
    from flakefinder.model import atomic_json
    scope,result=run_discovery(tmp_path,5000.)
    record=result['records'][1]
    edge,_=measure_rectangle_side(load_frame(tmp_path/record['image']),scope.transform.matrix,[1,0])
    assert edge is not None  # Reproduce the old RGB-only acceptance in the saved record.
    edge.update(image=record['image'],phase=record['phase'],position_um=record['position_um'])
    record['edge']=edge
    atomic_json(tmp_path/'discovery.json',result)
    before=scope.sequence
    for source_change in (True,False):
        replay=discover_sparse_outline(scope,[5000,5000],scope.transform,scope.reference_frame,tmp_path,scope.edge_options,
            edge_validation_profile=profile_for(scope.reference_frame),resume=True,continue_unresolved=True,
            allow_source_change=source_change)
        assert replay['state']=='estimated'
        assert not replay['boundary_revalidation'][record['image']]['material_support']['accepted']
        assert scope.sequence==before
        assert np.allclose(replay['estimated_outline']['measured_dimensions_um'],[5000,5000],atol=30)
        saved=json.loads((tmp_path/'discovery.json').read_text())
        assert saved['records'][1]['edge']==edge  # Immutable old observation retained, never trusted again.
