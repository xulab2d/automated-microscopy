import hashlib
import io
import json
import zipfile

import pytest

from flakefinder.model import SafetyError
from flakefinder.tile_sidecar_backup import create_tile_sidecar_backup,inspect_tile_sidecar_backup,restore_tile_sidecars
from test_full_chip import planned,fast_focus


def fixture(tmp_path):
    name='tile_000023'
    payloads={f'tiles/{name}.json':b'{"original": 1}\n',
              f'tiles/{name}.flakes.json':b'{"flakes": []}',
              f'boundary-labels/{name}.npz':b'original compressed label bytes',
              f'tiles/{name}.zen-vendor.json':b'{"original_receipt": true}'}
    hashes={}
    for relative,data in payloads.items():
        path=tmp_path/relative;path.parent.mkdir(exist_ok=True);path.write_bytes(data)
        hashes[relative]=hashlib.sha256(data).hexdigest()
    hashes[f'tiles/{name}.tiff']='a'*64
    row={'hashes':hashes,'sidecar_backup':create_tile_sidecar_backup(tmp_path,name,hashes)}
    return row,payloads


def test_exact_bundle_read_and_missing_only_restore(tmp_path):
    row,payloads=fixture(tmp_path)
    assert inspect_tile_sidecar_backup(tmp_path,row)==payloads
    with zipfile.ZipFile(tmp_path/row['sidecar_backup']['path']) as archive:
        assert set(archive.namelist())==set(payloads)
        assert not any(p.endswith('.tiff') for p in archive.namelist())
    missing=list(payloads)[:2]
    for p in missing:(tmp_path/p).unlink()
    assert inspect_tile_sidecar_backup(tmp_path,row)==payloads
    assert not any((tmp_path/p).exists() for p in missing) # Inspection never writes.
    assert set(restore_tile_sidecars(tmp_path,row))==set(missing)
    assert all((tmp_path/p).read_bytes()==data for p,data in payloads.items())
    assert restore_tile_sidecars(tmp_path,row)==[]
    assert inspect_tile_sidecar_backup(tmp_path,{'hashes':row['hashes']})=={}


def test_conflicting_existing_file_rejects_before_any_restore(tmp_path):
    row,payloads=fixture(tmp_path);missing,changed=list(payloads)[:2]
    (tmp_path/missing).unlink();(tmp_path/changed).write_bytes(b'changed')
    with pytest.raises(SafetyError,match='Existing sidecar changed'):restore_tile_sidecars(tmp_path,row)
    assert not (tmp_path/missing).exists() and (tmp_path/changed).read_bytes()==b'changed'


@pytest.mark.parametrize('mutation',['bundle','member','path','duplicate','large'])
def test_bundle_integrity_and_path_validation(tmp_path,mutation,monkeypatch):
    import flakefinder.tile_sidecar_backup as backup
    row,payloads=fixture(tmp_path);path=tmp_path/row['sidecar_backup']['path']
    if mutation=='bundle':path.write_bytes(b'corrupt')
    elif mutation=='path':row['sidecar_backup']['path']='../outside.zip'
    elif mutation=='large':monkeypatch.setattr(backup,'MAX_BUNDLE_BYTES',10)
    else:
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            for p,data in payloads.items():archive.writestr(p,b'changed' if mutation=='member' else data)
            if mutation=='duplicate':archive.writestr('../outside',b'bad')
        blob=output.getvalue();path.write_bytes(blob)
        row['sidecar_backup'].update(bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())
        new_relative=f'tile-evidence/tile_000023.{row["sidecar_backup"]["sha256"]}.zip'
        path.rename(tmp_path/new_relative);row['sidecar_backup']['path']=new_relative
    with pytest.raises(SafetyError):inspect_tile_sidecar_backup(tmp_path,row)


def test_create_refuses_changed_original_and_preserves_existing_bundle(tmp_path):
    row,payloads=fixture(tmp_path);name='tile_000023';path=tmp_path/row['sidecar_backup']['path']
    before=path.read_bytes()
    assert create_tile_sidecar_backup(tmp_path,name,row['hashes'])==row['sidecar_backup']
    assert path.read_bytes()==before
    (tmp_path/next(iter(payloads))).write_bytes(b'changed')
    with pytest.raises(SafetyError):create_tile_sidecar_backup(tmp_path,name,row['hashes'])
    assert path.read_bytes()==before


def test_raster_commits_bundle_reference_before_first_tile_callback(planned,fast_focus):
    import flakefinder.full_chip as full
    from flakefinder.model import Cancelled
    scope,reference,plan,folder=planned
    def stopped(phase,frame,info):
        if phase!='tile':return
        manifest=json.loads((folder/'manifest.json').read_text())
        row=next(iter(manifest['completed'].values()))
        assert len(inspect_tile_sidecar_backup(folder,row))==3
        raise Cancelled('Stop after verifying first committed bundle')
    with pytest.raises(Cancelled):full.run_full_chip(scope,plan,reference,folder,callback=stopped)
    assert len(json.loads((folder/'manifest.json').read_text())['completed'])==1


def test_stop_after_bundle_before_manifest_preserves_orphan_and_allows_recapture(planned,fast_focus,monkeypatch):
    import flakefinder.full_chip as full
    import flakefinder.tile_sidecar_backup as backup
    from flakefinder.model import Cancelled
    scope,reference,plan,folder=planned;create=backup.create_tile_sidecar_backup;orphan={}
    def interrupted(*args):
        ref=create(*args);orphan.update(reference=ref,bytes=(folder/ref['path']).read_bytes())
        raise Cancelled('Crash boundary: bundle durable but tile row not committed')
    monkeypatch.setattr(backup,'create_tile_sidecar_backup',interrupted)
    with pytest.raises(Cancelled):full.run_full_chip(scope,plan,reference,folder)
    assert json.loads((folder/'manifest.json').read_text())['completed']=={}
    monkeypatch.setattr(backup,'create_tile_sidecar_backup',create)
    scope.stopped=False;scope.armed=True
    def stop_after_commit(phase,frame,info):
        if phase=='tile':raise Cancelled('Stop after successfully recapturing orphan tile')
    with pytest.raises(Cancelled):full.run_full_chip(scope,plan,reference,folder,resume=True,callback=stop_after_commit)
    completed=json.loads((folder/'manifest.json').read_text())['completed']
    assert len(completed)==1
    row=next(iter(completed.values()))
    assert row['sidecar_backup']['path']!=orphan['reference']['path']
    assert (folder/orphan['reference']['path']).read_bytes()==orphan['bytes']
    assert inspect_tile_sidecar_backup(folder,row)
    # Repeating the committed content is idempotent and preserves both bundles.
    current=(folder/row['sidecar_backup']['path']).read_bytes()
    assert create(folder,'tile_000000',row['hashes'])==row['sidecar_backup']
    assert (folder/row['sidecar_backup']['path']).read_bytes()==current
    assert (folder/orphan['reference']['path']).read_bytes()==orphan['bytes']
