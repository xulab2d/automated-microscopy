"""Focus-before-line-fit control flow using an image simulator, no hardware."""
from pathlib import Path
import pytest
import flakefinder.sparse_acquisition as sparse
import flakefinder.focus_recovery as recovery
from flakefinder.autofocus import FocusResult
from flakefinder.model import AcquisitionError,Cancelled,Position,SafetyError
from test_chip_discovery import RECT
from sparse_profile_fixture import ImageStage,profile_for


def prepare(monkeypatch):
    scope=ImageStage([RECT]);reference=scope.capture();scope.did_focus=False
    scope.move_z=lambda z:setattr(scope,'p',Position(scope.p.x,scope.p.y,z))
    original=sparse.validated_coherent_edge
    monkeypatch.setattr(sparse,'validated_coherent_edge',lambda *args,**kwargs:original(*args,**kwargs) if scope.did_focus else (None,{'accepted':False,'reason':'synthetic defocus'}))
    options=sparse.SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    return scope,reference,options


def test_one_focus_precedes_fresh_line_fit_and_preserves_source(monkeypatch,tmp_path):
    scope,reference,options=prepare(monkeypatch);calls=[]
    def focus(adapter,low,high,output,callback,**budget):
        assert (low,high)==(60,140)
        frame=adapter.capture();assert frame.metadata['focus_mask_policy']=='include_chip_edge'
        calls.append(frame.position);scope.did_focus=True;scope.move_z(105)
        callback(adapter.capture(),{'phase':'confirmation','score':1.,'z_um':105})
        return FocusResult(True,105,1,1,'Synthetic verified focus',[])
    monkeypatch.setattr(recovery,'recover_focus',focus)
    result=sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),focus_search_bounds_um=[60,140])
    assert result['state']=='estimated' and len(calls)==1
    measured=result['edge_focus_attempts'][0]
    assert measured['state']=='focused' and measured['result_sha256']
    original=next(r for r in result['records'] if r['image']==measured['source_image'])
    fresh=next(r for r in result['records'] if r['image']==measured['focused_image'])
    assert original['position_um'][2]==100 and original['edge'] is None
    assert fresh['position_um'][2]==105 and fresh['edge']
    assert original['requested_xy_um']==fresh['requested_xy_um']
    before=scope.sequence
    replay=sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),
        resume=True,focus_search_bounds_um=[60,140])
    assert replay['state']=='estimated' and len(calls)==1 and scope.sequence==before


def test_unresolved_focus_stays_unresolved_without_hardware_stop(monkeypatch,tmp_path):
    scope,reference,options=prepare(monkeypatch)
    monkeypatch.setattr(recovery,'recover_focus',lambda *a,**k:FocusResult(False,100,0,0,'No peak',[]))
    result=sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),focus_search_bounds_um=[60,140])
    assert result['state']=='unresolved' and result['reason']=='edge_focus_unresolved'
    assert result['estimated_outline']['status']=='unresolved' and not result['estimated_outline']['polygon_um']
    assert len(result['edge_focus_attempts'])==1 and not scope.stopped


@pytest.mark.parametrize('cancel',[False,True])
def test_focus_hardware_error_or_stop_is_not_swallowed(monkeypatch,tmp_path,cancel):
    scope,reference,options=prepare(monkeypatch)
    def focus(adapter,*args,**kwargs):
        if cancel:
            (tmp_path/'STOP').touch();adapter.check_cancel()
        raise AcquisitionError('Camera transfer failed')
    monkeypatch.setattr(recovery,'recover_focus',focus)
    with pytest.raises(Cancelled if cancel else AcquisitionError):
        sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),focus_search_bounds_um=[60,140])
    assert scope.stopped


def test_source_only_resume_does_not_relax_configuration(monkeypatch,tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    options=sparse.SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,max_probes=4,cell_um=5,margin_um=20)
    original=sparse.file_hash;version=['old']
    monkeypatch.setattr(sparse,'file_hash',lambda path:version[0] if Path(path)==Path(sparse.__file__) else original(path))
    sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference))
    version[0]='new'
    with pytest.raises(SafetyError,match='resume configuration'):
        sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),resume=True)
    result=sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),resume=True,allow_source_change=True)
    assert result['execution_changes'][-1]['kind']=='explicit_source_transition'
    options.margin_um+=1
    with pytest.raises(SafetyError,match='resume configuration'):
        sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference),resume=True,allow_source_change=True)


def test_rejected_label_proposal_guides_focus_but_only_fresh_raw_fit_becomes_edge(monkeypatch,tmp_path):
    import flakefinder.full_chip as full_chip
    scope=ImageStage([RECT]);reference=scope.capture();scope.did_focus=False
    scope.move_z=lambda z:setattr(scope,'p',Position(scope.p.x,scope.p.y,z))
    original_fit=sparse.validated_coherent_edge;original_roi=full_chip.edge_focus_roi
    def fit(*args,**kwargs):
        edge,validation=original_fit(*args,**kwargs)
        if scope.did_focus:return edge,validation
        return None,{'accepted':False,'reason':'synthetic blur','original_label_proposal':edge}
    def select(*args,**kwargs):
        if not kwargs.get('boundary_proposal'):raise AcquisitionError('synthetic image-wide tensor failure')
        return original_roi(*args,**kwargs)
    def focus(adapter,*args,**kwargs):
        assert adapter.selection['method']=='label_guided_green_transition'
        assert adapter.selection['local_focus_verified'] is False
        scope.did_focus=True;scope.move_z(105)
        return FocusResult(True,105,1,1,'Synthetic focus confirmation',[])
    monkeypatch.setattr(sparse,'validated_coherent_edge',fit)
    monkeypatch.setattr(full_chip,'edge_focus_roi',select)
    monkeypatch.setattr(recovery,'recover_focus',focus)
    result=sparse.discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp_path,
        sparse.SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20),
        edge_validation_profile=profile_for(reference),focus_search_bounds_um=[60,140])
    attempt=result['edge_focus_attempts'][0]
    before=next(r for r in result['records'] if r['image']==attempt['source_image'])
    after=next(r for r in result['records'] if r['image']==attempt['focused_image'])
    assert before['edge'] is None and before['edge_validation']['accepted'] is False
    assert after['edge'] and after['position_um'][2]==105
    assert result['state']=='estimated'
