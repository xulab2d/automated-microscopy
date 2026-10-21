"""The upgrade is pinned in a new request, never written over its donor."""
import json
import sys
from types import SimpleNamespace

import pytest

from test_scan_service import service, setup


def test_rectangle_upgrade_verifies_before_writing_and_pins_request(service, monkeypatch):
    api, _, calls, _ = service
    contract = {'schema': 1, 'prior_run_id': 'scan-parent', 'deadline_at': 2000000000,
                'settings': api.preflight(setup())['settings']}
    verified = []
    def verify(project, value):
        assert not list((project/'data').glob('*/scan-request.json'))
        verified.append(value)
    monkeypatch.setitem(sys.modules, 'flakefinder.rectangle_handoff', SimpleNamespace(verify_contract=verify))
    result = api.plan(setup(), rectangle_handoff=contract)
    request = json.loads((api.folder(result['run']['id'])/'scan-request.json').read_text())
    assert verified == [contract] and request['rectangle_handoff'] == contract
    assert not calls


@pytest.mark.parametrize('other', ['reference_reuse', 'focus_first'])
def test_rectangle_upgrade_rejects_mixed_startup_modes(service, other):
    api, _, calls, _ = service
    with pytest.raises(ValueError, match='own saved campaign'):
        api.plan(setup(), rectangle_handoff={'schema': 1}, **{other: {'schema': 1}})
    assert not list((api.project/'data').glob('*/scan-request.json'))
    assert not calls
