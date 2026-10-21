"""Actual clipping oscillation replay; saved acquisition files are read-only."""
import json
from pathlib import Path

import pytest

from flakefinder import scan_worker as worker
from flakefinder.imaging import load_frame
from flakefinder.vendor_lifecycle import file_hash


@pytest.mark.parametrize('candidate', [2, 3])
def test_saved_hbn_usable_second_exposure_is_selected_and_validated(candidate, tmp_path, monkeypatch):
    run=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy\data\scan-20261015-173045-69478f68')
    proof_path=run/'autonomous-reference/proof.json'
    if not proof_path.exists():pytest.skip('Saved lab exposure replay unavailable')
    proof=json.loads(proof_path.read_bytes())
    attempts=proof['reference_candidates'][candidate-1]['exposure_audit']['attempts']
    if any(not Path(row['raw_path']).exists() for row in attempts[:2]):
        pytest.skip('Saved lab reference raw unavailable')
    for row in attempts[:2]:
        path=Path(row['raw_path'])
        assert file_hash(path)==row['raw_sha256']
        assert file_hash(path.with_suffix('.json'))==row['metadata_sha256']
    request=json.loads((run/'scan-request.json').read_bytes())
    profile=json.loads((run/'camera-profile-used.json').read_bytes())
    settings=dict(request['settings'],exposure_ms=attempts[0]['exposure_ms'])
    config,_=worker.fresh_config(profile,settings,tmp_path,run.name)
    position=load_frame(Path(attempts[0]['raw_path'])).position

    class SavedCapture:
        captures=0
        exposure=None

        def set_exposure_ms(self,value):
            self.exposure=value

        def capture(self):
            # The third original attempt clipped again. The repaired policy
            # must stop at the independently usable second measurement.
            assert self.captures<2
            row=attempts[self.captures]
            assert self.exposure==pytest.approx(row['exposure_ms'],abs=.0001)
            self.captures+=1
            return load_frame(Path(row['raw_path']))

    scope=SavedCapture()
    monkeypatch.setattr('flakefinder.vendor_lifecycle.acknowledge_raw',
        lambda _scope,frame,path:{'status':'offline_replay','raw_tiff':str(path)})
    frame,audit,receipt=worker._capture_reference(scope,config,settings,tmp_path,position,
        lambda:None,lambda *args,**kwargs:None)
    assert scope.captures==2
    assert audit['chosen_attempt']==2
    assert audit['photometrically_usable'] and not audit['converged']
    assert audit['choice']=='best_usable_capture'
    assert config['context']['exposure_ms']==attempts[1]['exposure_ms']==scope.exposure
    assert frame.metadata['camera_resources']==config['zen']['camera_resources']
    assert Path(receipt['raw_tiff']).is_relative_to(tmp_path)
    _,quality=worker.validate_reference(frame,config)
    assert quality['substrate_color_support_fraction']>.96
    assert quality['clipped_fraction']<=.001
    assert file_hash(Path(attempts[1]['raw_path']))==attempts[1]['raw_sha256']
