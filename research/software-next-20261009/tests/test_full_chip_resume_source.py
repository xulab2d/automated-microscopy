import copy
import hashlib
import json

import pytest

from flakefinder.full_chip import _accept_resume_payload
from flakefinder.model import SafetyError


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def test_resume_code_repair_requires_explicit_flag_and_preserves_evidence():
    old={'schema':1,'limits':{'z_max':100},'algorithm_source_sha256':'old'}
    new={**old,'algorithm_source_sha256':'new'}
    manifest={**old,'fingerprint':fingerprint(old),'completed':{'tile_0':{'hashes':{'image':'unchanged'}}}}
    with pytest.raises(SafetyError,match='differs'):
        _accept_resume_payload(manifest,new,fingerprint(new))
    evidence=copy.deepcopy(manifest['completed'])
    _accept_resume_payload(manifest,new,fingerprint(new),True)
    assert manifest['completed']==evidence
    assert manifest['algorithm_transitions'][0]['from_sha256']=='old'
    assert manifest['fingerprint']==fingerprint(new)


def test_resume_code_repair_does_not_authorize_changed_limits_or_tampered_provenance():
    old={'schema':1,'limits':{'z_max':100},'algorithm_source_sha256':'old'}
    new={**old,'limits':{'z_max':200},'algorithm_source_sha256':'new'}
    manifest={**old,'fingerprint':fingerprint(old)}
    with pytest.raises(SafetyError,match='differs'):
        _accept_resume_payload(manifest,new,fingerprint(new),True)
    manifest['limits']={'z_max':200}
    with pytest.raises(SafetyError,match='fingerprint is invalid'):
        _accept_resume_payload(manifest,new,fingerprint(new),True)
