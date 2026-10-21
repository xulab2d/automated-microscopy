"""Deadline enforcement with fake time and files only; no worker/hardware."""
from datetime import datetime, timezone
import argparse
import json
import pytest
from scripts.guard_campaign_deadline import guard, parse_utc, create_stop
import scripts.guard_campaign_deadline as deadline_guard


@pytest.fixture(autouse=True)
def no_os_sleep_changes(monkeypatch):
    monkeypatch.setattr(deadline_guard, 'keep_system_awake', lambda enable: False)


def setup(tmp_path, state='running'):
    batch = {'coordinate_frame_id': 'campaign-a', 'state': state, 'current_chip': 'chip7'}
    (tmp_path / 'batch.json').write_text(json.dumps(batch), encoding='utf-8')
    return batch


def test_deadline_requests_stop_during_active_chip_without_editing_batch(tmp_path):
    setup(tmp_path)
    before = (tmp_path / 'batch.json').read_bytes()
    now = [100.]
    assert guard(tmp_path, 'campaign-a', datetime.fromtimestamp(112, timezone.utc),
                 clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds)) == 0
    assert now[0] == 112
    assert json.loads((tmp_path / 'STOP').read_text())['coordinate_frame_id'] == 'campaign-a'
    assert (tmp_path / 'batch.json').read_bytes() == before
    assert json.loads((tmp_path / 'deadline-guard.json').read_text())['stop_created']


def test_existing_operator_stop_is_never_overwritten(tmp_path):
    setup(tmp_path)
    (tmp_path / 'STOP').write_text('Operator STOP; keep this exact text', encoding='utf-8')
    deadline = datetime.fromtimestamp(1, timezone.utc)
    assert not create_stop(tmp_path, 'campaign-a', deadline)
    assert guard(tmp_path, 'campaign-a', deadline, clock=lambda: 2) == 0
    assert (tmp_path / 'STOP').read_text() == 'Operator STOP; keep this exact text'


def test_budget_pause_and_extension_remain_guarded(tmp_path):
    batch = setup(tmp_path, 'time_budget_reached');now=[100.];states=[]
    def sleep(seconds):
        states.append(json.loads((tmp_path/'deadline-guard.json').read_text())['state'])
        now[0] += seconds
        batch['state'] = 'running'
        (tmp_path/'batch.json').write_text(json.dumps(batch), encoding='utf-8')
    assert guard(tmp_path, 'campaign-a', datetime.fromtimestamp(111,timezone.utc), clock=lambda:now[0], sleep=sleep)==0
    assert 'waiting_at_budget_pause' in states and 'guarding' in states and (tmp_path/'STOP').exists()


def test_coordinate_change_exits_without_stopping_new_campaign(tmp_path):
    batch=setup(tmp_path);now=[100.]
    def sleep(seconds):
        now[0]+=seconds;batch['coordinate_frame_id']='campaign-b'
        (tmp_path/'batch.json').write_text(json.dumps(batch),encoding='utf-8')
    assert guard(tmp_path,'campaign-a',datetime.fromtimestamp(106,timezone.utc),clock=lambda:now[0],sleep=sleep)==2
    assert not (tmp_path/'STOP').exists()


def test_terminal_campaign_exits_before_deadline(tmp_path):
    setup(tmp_path,'search_sampling_complete')
    assert guard(tmp_path,'campaign-a',datetime.fromtimestamp(200,timezone.utc),clock=lambda:100)==0
    assert not (tmp_path/'STOP').exists()


def test_unreadable_progress_does_not_disarm_deadline(tmp_path):
    setup(tmp_path);now=[100.]
    def sleep(seconds):
        now[0]+=seconds;(tmp_path/'batch.json').write_text('{',encoding='utf-8')
    assert guard(tmp_path,'campaign-a',datetime.fromtimestamp(106,timezone.utc),clock=lambda:now[0],sleep=sleep)==0
    assert (tmp_path/'STOP').exists()


def test_utc_deadline_is_explicit_and_correct():
    assert parse_utc('2026-10-09T16:00:00Z').isoformat()=='2026-10-09T16:00:00+00:00'
    for invalid in ('2026-10-09T09:00:00','2026-10-09T09:00:00-07:00'):
        with pytest.raises(argparse.ArgumentTypeError):parse_utc(invalid)
