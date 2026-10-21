"""Imported observations remain immutable and never renew old acquisition budgets."""
from pathlib import Path
import json
import pytest
from sparse_profile_fixture import ImageStage,profile_for
from test_chip_discovery import RECT
from flakefinder.sparse_acquisition import SparseEdgeOptions,discover_sparse_outline
from flakefinder.sparse_import import prepare_discovery_import,digest
from flakefinder.model import SafetyError

def prepare(tmp_path,additional):
    scope=ImageStage([RECT]);ref=scope.capture();profile=profile_for(ref)
    options=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,max_probes=2,cell_um=5,margin_um=20)
    source=tmp_path/'original';discover_sparse_outline(scope,[500,500],scope.transform,ref,source,options,edge_validation_profile=profile)
    before=(source/'discovery.json').read_bytes();new=tmp_path/'derived'
    imported=prepare_discovery_import(source,new,ref,scope.transform,scope.limits,profile,additional_probes=additional)
    # This fixture owns the same synthetic stage instance before/after import.
    # Give ONLY that simulation a verified-placement contract; real prepared
    # imports intentionally remain historical and non-executable by default.
    imported['context']['restart_placement_status']='verified_current'
    runtime_keys={'fingerprint','state','started_at','updated_at','records','imported_records','rays','estimated_outline','lineage'}
    imported['fingerprint']=digest({k:v for k,v in imported.items()if k not in runtime_keys})
    (new/'discovery.json').write_text(json.dumps(imported))
    return scope,ref,profile,source,before,new,imported

def test_explicit_supplement_reuses_old_observations_without_recapturing(tmp_path):
    scope,ref,profile,source,before,new,imported=prepare(tmp_path,1)
    count=scope.sequence;old_imports=json.dumps(imported['imported_records'],sort_keys=True)
    result=discover_sparse_outline(scope,[500,500],scope.transform,ref,new,SparseEdgeOptions(**imported['options']),
        context=imported['context'],resume=True,edge_validation_profile=profile)
    assert len(result['records'])==1 and scope.sequence==count+1
    assert result['lineage']['old_used']==2 and result['lineage']['explicit_additional_allowance']==1
    assert (source/'discovery.json').read_bytes()==before
    assert json.dumps(result['imported_records'],sort_keys=True)==old_imports
    # A second resume has no allowance left and captures nothing.
    again=discover_sparse_outline(scope,[500,500],scope.transform,ref,new,SparseEdgeOptions(**imported['options']),
        context=imported['context'],resume=True,edge_validation_profile=profile)
    assert len(again['records'])==1 and scope.sequence==count+1

def test_exhausted_budget_has_no_silent_single_frame_allowance(tmp_path):
    scope,ref,profile,source,before,new,imported=prepare(tmp_path,0);moves=len(scope.moves)
    assert imported['lineage']['new_probe_allowance']==0 and not imported['lineage']['new_acquisition_allowed']
    with pytest.raises(SafetyError,match='positive new probe allowance'):
        discover_sparse_outline(scope,[500,500],scope.transform,ref,new,SparseEdgeOptions(**imported['options']),
            context=imported['context'],resume=True,edge_validation_profile=profile)
    assert len(scope.moves)==moves and (source/'discovery.json').read_bytes()==before

def test_imported_metadata_tampering_rejected_before_motion(tmp_path):
    scope,ref,profile,source,before,new,imported=prepare(tmp_path,1);moves=len(scope.moves)
    state=json.loads((new/'discovery.json').read_text());state['imported_records'][0]['position_um'][0]+=2
    (new/'discovery.json').write_text(json.dumps(state))
    with pytest.raises(SafetyError,match='fingerprint-bound restart context'):
        discover_sparse_outline(scope,[500,500],scope.transform,ref,new,SparseEdgeOptions(**imported['options']),
            context=imported['context'],resume=True,edge_validation_profile=profile)
    assert len(scope.moves)==moves

def test_historical_import_is_not_a_placement_verification(tmp_path):
    scope,ref,profile,source,before,new,imported=prepare(tmp_path,1)
    historical=prepare_discovery_import(source,tmp_path/'historical',ref,scope.transform,scope.limits,profile,additional_probes=1)
    moves=len(scope.moves)
    with pytest.raises(SafetyError,match='Imported XY evidence is historical'):
        discover_sparse_outline(scope,[500,500],scope.transform,ref,tmp_path/'historical',SparseEdgeOptions(**historical['options']),
            context=historical['context'],resume=True,edge_validation_profile=profile)
    assert len(scope.moves)==moves
