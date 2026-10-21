import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import parallel_graphene_refinement as parallel
import analyze_graphene_run as driver


def test_refinement_requires_exact_policy_source_and_target(tmp_path):
    source=tmp_path/'project'/'analysis'/'graphene'/'run'/'chip001'/'base';source.mkdir(parents=True)
    driver.watcher.save(source/'plan.json',{})
    args=SimpleNamespace(analysis=source,version='new')
    with pytest.raises(ValueError,match='does not match'):
        parallel.run(args,{'schema':1,'workers':2,'version':'other','source_plan_sha256':driver.digest(source/'plan.json')})
    assert not (source.parent/'new').exists()


def test_worker_missing_shard_has_no_parent_ledger_write(tmp_path,monkeypatch):
    monkeypatch.setattr(driver,'cpu_policy',lambda **kw:{})
    out=tmp_path/'output';out.mkdir();driver.watcher.save(out/'ledger.json',{'previous':'preserved'})
    entry={'field_json':'missing.json','hashes':{'missing.json':'a'*64}}
    result=parallel.process_field(tmp_path,tmp_path/'chip001','tile_000001',entry,out,{'run_id':'run'},'b'*64,None)
    assert result['state']=='source_unavailable'
    assert driver.read(out/'ledger.json')=={'previous':'preserved'}


def test_worker_obeys_stop_without_creating_derived_field(tmp_path,monkeypatch):
    monkeypatch.setattr(driver,'cpu_policy',lambda **kw:{})
    out=tmp_path/'output';out.mkdir();(out/'STOP').touch()
    with pytest.raises(driver.watcher.AnalysisStop):
        parallel.process_field(tmp_path,tmp_path/'chip001','tile_000001',{},out,{'run_id':'run'},'b'*64,None)
    assert not (out/'fields').exists()
