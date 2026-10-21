import json,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import offline_graphene_parallel as parallel
import analyze_graphene_run as driver


def test_two_bounded_workers_commit_only_verified_records(tmp_path,monkeypatch):
    from flakefinder import memory_capacity
    monkeypatch.setattr(driver,'cpu_policy',lambda **kwargs:{})
    monkeypatch.setattr(memory_capacity,'snapshot',lambda:{'physical_available_bytes':1,'commit_available_bytes':4*1024**3,'address_available_bytes':4*1024**3})
    def analyze(entry,model,attempt,args,check):
        check();attempt.mkdir(parents=True);value={'assessment_state':'assessed','regions':[]};driver.watcher.save(attempt/'field.json',value);return value
    monkeypatch.setattr(driver,'analyze_field',analyze)
    tiles=['tile_000000','tile_000001','tile_000002'];manifest={'completed':{t:{'hashes':{}} for t in tiles},'transform':{}}
    ledger={};state={};calls=[]
    parallel.run_fields(tiles,tmp_path,manifest,{},SimpleNamespace(field_timeout=30),tmp_path,ledger,state,
        check=lambda:None,status=lambda:None,verify_sources=lambda:calls.append('verified'),cpu_masks=[1,2],executor_type=ThreadPoolExecutor)
    assert list(ledger)==tiles and state['state']=='complete'
    assert len(calls)==5
    assert state['memory_capacity']['adequate']  # low free physical RAM alone is not a veto
    assert all(not isinstance(v,list) for r in ledger.values() for v in r.values())


def test_changed_source_before_commit_leaves_field_uncommitted(tmp_path,monkeypatch):
    from flakefinder import memory_capacity
    monkeypatch.setattr(driver,'cpu_policy',lambda **kwargs:{})
    monkeypatch.setattr(memory_capacity,'snapshot',lambda:{'physical_available_bytes':1,'commit_available_bytes':4*1024**3,'address_available_bytes':4*1024**3})
    def analyze(entry,model,attempt,args,check):
        attempt.mkdir(parents=True);value={'assessment_state':'assessed','regions':[]};driver.watcher.save(attempt/'field.json',value);return value
    monkeypatch.setattr(driver,'analyze_field',analyze)
    calls=[]
    def verify():
        calls.append(1)
        if len(calls)>1:raise ValueError('Source changed')
    ledger={};manifest={'completed':{'tile_000000':{'hashes':{}}},'transform':{}}
    with pytest.raises(ValueError,match='Source changed'):
        parallel.run_fields(['tile_000000'],tmp_path,manifest,{},SimpleNamespace(field_timeout=30),tmp_path,ledger,{},
            check=lambda:None,status=lambda:None,verify_sources=verify,cpu_masks=[1,2],executor_type=ThreadPoolExecutor)
    assert ledger=={}


def test_child_stop_is_not_committed_as_scientific_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(driver,'cpu_policy',lambda **kwargs:{})
    (tmp_path/'STOP').touch()
    entry={'field':'chip001/tile_000000','source_hashes':{}}
    with pytest.raises(driver.watcher.AnalysisStop):
        parallel.process_field(entry,{},tmp_path/'attempt',SimpleNamespace(field_timeout=30),tmp_path,1)
    assert not (tmp_path/'attempt'/'field.json').exists()
