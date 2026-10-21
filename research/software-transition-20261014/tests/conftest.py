"""Offline tests must never inherit the lab's real Drive routing policy.

Individual storage-policy tests install their own temporary policy explicitly.
Production storage code and operator-hold checks remain unchanged.
"""
import pytest
import flakefinder.storage as storage
import flakefinder.lease as lease


@pytest.fixture(autouse=True)
def isolated_storage_policy(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'POLICY_FILE',tmp_path/'no-lab-policy.json')
    monkeypatch.delenv('FLAKEFINDER_STORAGE_POLICY',raising=False)
    monkeypatch.delenv('FLAKEFINDER_PROJECT_ROOT',raising=False)
    monkeypatch.setattr(lease,'host_lease_directory',lambda:tmp_path/'host-leases')
