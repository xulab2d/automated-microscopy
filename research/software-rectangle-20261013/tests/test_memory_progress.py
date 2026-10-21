from dataclasses import asdict
from pathlib import Path
import json
import pytest
from sparse_profile_fixture import ImageStage,profile_for
from test_chip_discovery import RECT
from flakefinder import memory_progress as progress
from flakefinder.model import atomic_json,SafetyError
from flakefinder.chip_discovery import file_hash
from flakefinder.sparse_acquisition import SparseEdgeOptions,discover_sparse_outline


def fixture(tmp_path):
    parent=tmp_path/'data/scan-parent';out=tmp_path/'data/scan-new';out.mkdir(parents=True)
    scope=ImageStage([RECT]);ref=scope.capture();source=parent/'discovery/candidate002'
    opts=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,max_probes=5,cell_um=5,margin_um=20)
    old=discover_sparse_outline(scope,[500,500],scope.transform,ref,source,opts,edge_validation_profile=profile_for(ref))
    old.update(state='failed');old['options']['max_probes']=10
    atomic_json(source/'discovery.json',old)
    seed={'index':1,'requested_xy_um':[500,500],'image':'search/frames/seed.tiff','hashes':{}}
    from flakefinder.imaging import save_frame
    save_frame(parent/'search/frames','seed',ref)
    seed['hashes']={f'search/frames/seed.{ext}':file_hash(parent/f'search/frames/seed.{ext}') for ext in ('tiff','json')}
    batch={'state':'failed','coordinate_frame_id':'frame','search_bounds_um':[20,1200,20,1100],
        'chip_states':{},'current_chip':None,'seed_records':[seed],
        'boundary_attempts':[{'id':'candidate002','folder':str(source),'seed_record_index':0,'state':'discovering'}]}
    atomic_json(parent/'batch.json',batch);search={'probe_count':3};atomic_json(parent/'search-plan.json',search)
    paths=['batch.json']+progress.progress_files(parent,batch)
    contract={'prior_run_id':parent.name,'preserve_pre_raster_progress':True,
        'prior_files_sha256':{name:file_hash(parent/name) for name in paths}}
    atomic_json(out/'scan-request.json',{'continuation':contract});atomic_json(out/'continuation-verification.json',{'continuation':contract})
    cfg={'limits':asdict(scope.limits),'context':{'prior_run_id':parent.name},'chip_discovery':{'edge_validation_profile':profile_for(ref)}}
    state={'coordinate_frame_id':'frame','search_bounds_um':batch['search_bounds_um']}
    return parent,out,scope,ref,cfg,state,search,old


def test_preserves_cached_geometry_lattice_and_unused_budget(tmp_path):
    parent,out,scope,ref,cfg,state,search,old=fixture(tmp_path)
    before=(parent/'discovery/candidate002/discovery.json').read_bytes();count=scope.sequence
    restored=progress.prepare_progress(out,state,cfg,ref,scope.transform,search)
    assert scope.sequence==count and restored['pending']=={0,2}
    folder=out/'discovery/candidate002';imported=progress.read(folder/'discovery.json')
    assert len(imported['imported_records'])==len(old['records'])
    assert imported['options']['max_probes']==10-len(old['records'])
    assert imported['lineage']['recovery_operation_budget']['max_total_captures']==64-old['operation_budget']['captures_started']
    assert imported['context']['restart_placement_status']=='verified_current'
    for row in imported['imported_records']:
        assert all((folder/n).is_file() and not Path(n).is_absolute() for n in row['hashes'])
    result=discover_sparse_outline(scope,[500,500],scope.transform,ref,folder,SparseEdgeOptions(**imported['options']),
        context=imported['context'],resume=True,edge_validation_profile=profile_for(ref))
    assert result['estimated_outline']['evidence']['imported_probe_images']==len(old['records'])
    assert (parent/'discovery/candidate002/discovery.json').read_bytes()==before


@pytest.mark.parametrize('change',['proof','pixels','lattice','frame','completed'])
def test_refuses_unverified_or_changed_progress(tmp_path,change):
    parent,out,scope,ref,cfg,state,search,old=fixture(tmp_path)
    if change=='proof':atomic_json(out/'continuation-verification.json',{'continuation':{}})
    elif change=='pixels':(parent/'search/frames/seed.tiff').write_bytes(b'changed')
    elif change=='lattice':search['probe_count']=4
    elif change=='frame':state['coordinate_frame_id']='other'
    else:
        batch=progress.read(parent/'batch.json');batch['chip_states']={'chip001':{}};atomic_json(parent/'batch.json',batch)
    with pytest.raises(SafetyError):progress.prepare_progress(out,state,cfg,ref,scope.transform,search)
