import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N
import pytest

spec=importlib.util.spec_from_file_location('memory_watcher',Path(__file__).parents[1]/'scripts/watch_scan_memory.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_exact_100ns_identity_and_pressure_cadence():
    # Windows FILETIME epoch is1601, unlike .NET DateTime ticks (year1).
    assert m.start_ticks('2026-10-11T19:44:33.8590181Z')==134362214738590181
    assert m.cleanup_due(1,60,0) and not m.cleanup_due(1,59,0)
    assert not m.cleanup_due(3*1024**3,61,0)
    with pytest.raises(ValueError):m.start_ticks('not a timestamp')


@pytest.mark.parametrize('problem,expected',[
    ('closed','run_not_running'),('dead','worker_exited_or_unreadable'),('changed','run_identity_changed'),('expired','deadline_reached')])
def test_lifecycle_refuses_actions(problem,expected):
    status={'id':'run','pid':12,'state':'running','hardware_closed':False,'request_sha256':'sha'}
    if problem=='closed':status['hardware_closed']=True
    if problem=='changed':status['request_sha256']='other'
    assert m.lifecycle(status,'run',12,'sha',problem!='dead',100,99 if problem=='expired' else 200)==expected


def test_small_watcher_limits_actions_and_stops_with_worker(tmp_path):
    run=tmp_path/'run';run.mkdir();request=b'{}';(run/'scan-request.json').write_bytes(request)
    sha=hashlib.sha256(request).hexdigest()
    m.write(run/'worker-start.json',{'id':'run','pid':12,'request_sha256':sha})
    m.write(run/'scan-status.json',{'id':'run','pid':12,'state':'running','hardware_closed':False,'request_sha256':sha})
    now=[0];calls=[]
    host=N(matches=lambda *a:now[0]<80,available=lambda:1)
    result=m.monitor(run,12,'2026-10-11T19:44:33.8590181Z',200,lambda:calls.append(now[0]),host,
        clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
    assert calls==[0,60] and result=='worker_exited_or_unreadable'
    saved=m.read(run/'memory-guard/status.json')
    assert saved['hardware_access'] is False and saved['state']==result


@pytest.mark.parametrize('failure',[FileNotFoundError('replacement window'),OSError(22,'provider read'),json.JSONDecodeError('partial','',0)])
def test_missing_status_suspends_actions_then_recovers(tmp_path,monkeypatch,failure):
    run=tmp_path/'run';run.mkdir();(run/'scan-request.json').write_bytes(b'{}')
    sha=hashlib.sha256(b'{}').hexdigest()
    m.write(run/'worker-start.json',{'id':'run','pid':12,'request_sha256':sha})
    m.write(run/'scan-status.json',{'id':'run','pid':12,'request_sha256':sha,'state':'running'})
    now=[0];calls=[];original=m.read
    def flaky(path):
        if Path(path).name=='scan-status.json' and now[0]<20:raise failure
        return original(path)
    monkeypatch.setattr(m,'read',flaky)
    result=m.monitor(run,12,'2026-10-11T19:44:33.8590181Z',100,lambda:calls.append(now[0]),
        N(matches=lambda *a:now[0]<30,available=lambda:1),clock=lambda:now[0],
        sleep=lambda s:now.__setitem__(0,now[0]+s))
    assert calls==[20] and result=='worker_exited_or_unreadable'
    assert original(run/'memory-guard/status.json').get('cleanup_suspended') is None


def test_unreadable_status_and_report_still_honor_deadline(tmp_path,monkeypatch):
    run=tmp_path/'run';run.mkdir();(run/'scan-request.json').write_bytes(b'{}')
    sha=hashlib.sha256(b'{}').hexdigest()
    m.write(run/'worker-start.json',{'id':'run','pid':12,'request_sha256':sha})
    now=[0];calls=[];original=m.read
    def absent(path):
        if Path(path).name=='scan-status.json':raise FileNotFoundError('unavailable')
        return original(path)
    monkeypatch.setattr(m,'read',absent)
    monkeypatch.setattr(m,'write',lambda *a:(_ for _ in ()).throw(OSError(22,'unavailable')))
    result=m.monitor(run,12,'2026-10-11T19:44:33.8590181Z',20,lambda:calls.append(1),
        N(matches=lambda *a:True,available=lambda:1),clock=lambda:now[0],
        sleep=lambda s:now.__setitem__(0,now[0]+s))
    assert result=='deadline_reached' and not calls
