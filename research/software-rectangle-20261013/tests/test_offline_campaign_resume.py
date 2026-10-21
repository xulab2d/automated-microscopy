"""Coordinator regressions; synthetic saved fields, no microscope imports."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def coordinator(monkeypatch):
    pytest.importorskip('msvcrt')
    path = Path(__file__).parents[1] / 'research/residue-sieves-resume-v2/batch_sieves.py'
    spec = importlib.util.spec_from_file_location('resume_coordinator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'output_guard', lambda *a, **kw: None)
    monkeypatch.setattr(module, 'optical_profile', lambda *a: None)
    return module


def fixture_plan(module, tmp_path, errors):
    out = tmp_path/'campaign';source = out/'source';source.mkdir(parents=True)
    worker = source/'run_sieves_batch_worker.py'
    worker.write_text('''from pathlib import Path
import json,sys
BATCH_CHECK=lambda:None
BATCH_INPUT={}
AcquisitionError=type('AcquisitionError',(RuntimeError,),{'__module__':'flakefinder.model'})
def main():
    key=sys.argv[-1]
    error=json.loads((Path(__file__).parent/'errors.json').read_text()).get(key)
    if error:
        if error=='unexpected':raise RuntimeError('Unexpected implementation failure')
        raise AcquisitionError(error)
    out=Path(sys.argv[sys.argv.index('--output')+1]);(out/'fields').mkdir(parents=True)
    (out/'fields'/'field.json').write_text(json.dumps({'rows':[]}))
    (out/'completion.json').write_text(json.dumps({'summary':{'counts':{},'sieve_failures':{},'assessed_regions':0,'elapsed_seconds':0},'field_reports':[]}))
''')
    (source/'errors.json').write_text(json.dumps(errors))
    fields=[]
    for key in list(errors)+['chip/good']:
        raw=out/(key.replace('/','_')+'.raw');raw.write_bytes(b'retained source')
        fields.append({'field':key,'shard':str(raw),'mask':str(raw),'hashes':{str(raw):module.digest(raw)}})
    module.save(out/'plan.json',{'reserve_gib':8,'fields':fields,'source_failures':[],
        'source_hashes':{},'frozen_hashes':{},'campaign':str(out),'workspace':str(out),'supervisor_sha256':module.digest(module.__file__)})
    return out


def test_known_support_refusals_continue_and_resume_idempotently(coordinator,tmp_path):
    errors={
        'chip/one':'Insufficient independent substrate samples.',
        'chip/two':'No sufficient independently recorded bare-substrate support; no color measurement or zero-contrast pass.',
        'chip/three':'Substrate support cannot sustain spatial held-out fit validation.',
    }
    out=fixture_plan(coordinator,tmp_path,errors)
    coordinator.run(SimpleNamespace(output=out))
    status=json.loads((out/'status.json').read_text())
    assert status['state']=='complete_with_exclusions'
    assert (status['processed_fields'],status['successful_fields'],status['unassessable_fields'],status['analysis_failure_count'])==(4,1,3,0)
    ledger=json.loads((out/'ledger.json').read_text())
    report=json.loads((Path(ledger['chip/one']['folder'])/'quality-exclusion.json').read_text())
    assert report['source_features_retained'] and report['color_measurement_performed'] is False
    assert report['source_hashes']
    before=(out/'ledger.json').read_bytes()
    coordinator.run(SimpleNamespace(output=out))
    assert (out/'ledger.json').read_bytes()==before
    assert len(list((out/'field-attempts').iterdir()))==4


def test_unexpected_error_circuit_breaker_remains(coordinator,tmp_path):
    out=fixture_plan(coordinator,tmp_path,{f'chip/{i}':'unexpected' for i in range(3)})
    with pytest.raises(RuntimeError,match='three consecutive'):
        coordinator.run(SimpleNamespace(output=out))
    status=json.loads((out/'status.json').read_text())
    assert status['state']=='failed_closed' and status['analysis_failure_count']==3
    assert status['successful_fields']==0
    assert not (out/'ledger.json').exists()


def test_stop_and_changed_committed_source_still_stop(coordinator,tmp_path):
    out=fixture_plan(coordinator,tmp_path,{})
    (out/'STOP').write_text('operator stop')
    coordinator.run(SimpleNamespace(output=out))
    assert json.loads((out/'status.json').read_text())['state']=='stopped'
    assert not (out/'ledger.json').exists()
    # This is a synthetic fixture STOP only; production STOPs are never cleared.
    (out/'STOP').unlink()
    coordinator.run(SimpleNamespace(output=out))
    (out/'chip_good.raw').write_bytes(b'changed')
    with pytest.raises(ValueError,match='Source hash mismatch'):
        coordinator.run(SimpleNamespace(output=out))
    assert json.loads((out/'status.json').read_text())['state']=='failed_closed'


def test_quality_policy_does_not_catch_unknown_or_missing_sources(coordinator):
    Error=type('AcquisitionError',(RuntimeError,),{'__module__':'flakefinder.model'})
    for error in (Error('Missing or changed independently recorded substrate labels.'),
                  Error('Nonpositive supported-domain background; fit rejected.'),
                  ValueError('Source bytes differ from recorded masks'),
                  RuntimeError('Insufficient independent substrate samples.')):
        assert coordinator.quality_reason(error) is None
