"""Deep autofocus output must not lengthen the Win32 vendor-preview copy path."""
import json
from pathlib import Path

from test_zen_vendor_retention import persisted
from flakefinder import storage, vendor_lifecycle


def test_deep_focus_preview_copy_is_short_and_verified(persisted,tmp_path,monkeypatch):
    scope,frame,raw,vendor=persisted
    monkeypatch.setattr(storage,'load_policy',lambda:None)
    deep=tmp_path/('nested-focus-'+'a'*(190-len(str(tmp_path))-14))
    (vendor/'thumb.png').rename(vendor/'chip1-190.czi.thumb.png')
    assert len(str(deep/'observations'/(vendor.name+'.vendor-previews')/'chip1-190.czi.thumb.png'))>=260
    copy=vendor_lifecycle.shutil.copyfile
    destinations=[]
    def win32_copy(source,destination):
        destinations.append(Path(destination))
        assert len(str(destination))<260
        return copy(source,destination)
    monkeypatch.setattr(vendor_lifecycle.shutil,'copyfile',win32_copy)
    receipt=vendor_lifecycle.checkpoint_observation(scope,frame,deep,'focus',{'score':1.})
    assert receipt['status']=='removed' and not vendor.exists()
    archive=json.loads(Path(receipt['archive']).read_text())
    saved=archive['vendor_previews_preserved'][0]
    assert Path(saved['path']).read_bytes()==b'preview'
    assert vendor_lifecycle.file_hash(saved['path'])==saved['sha256']
    assert destinations[0].parent.parent.name=='observation-previews'
    assert raw.is_file() and Path(receipt['evidence']).is_file()


def test_nested_scan_observations_remain_within_run(persisted,tmp_path,monkeypatch):
    scope,frame,raw,vendor=persisted
    monkeypatch.setattr(storage,'load_policy',lambda:None)
    run=tmp_path/'data'/'scan-20261010-000000-12345678'
    deep=run/'chip001'/'focus-anchors'/'anchor_000'/'recovery'/'verification'/'adaptive-fine'
    receipt=vendor_lifecycle.checkpoint_observation(scope,frame,deep,'focus',{'score':1.})
    assert Path(receipt['evidence']).parent==run/'observations'
    evidence=json.loads(Path(receipt['evidence']).read_text())
    assert evidence['observation_context_folder']==str(deep)
    assert Path(evidence['preview']['path']).is_file() and raw.is_file()
