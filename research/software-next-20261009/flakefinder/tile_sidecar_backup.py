"""Redundant exact-byte tile sidecars; never a substitute for original raw TIFFs.

The optional manifest reference pins a separate ZIP, and the original manifest
hashes pin every member. This is local recovery redundancy on the same storage
provider, not a guarantee of cloud synchronization or independent backup.
"""
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile

from .model import SafetyError

MAX_BUNDLE_BYTES=32*1024*1024


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _path(folder,relative):
    if not isinstance(relative,str) or '\\' in relative:
        raise SafetyError('Sidecar path is not a canonical relative path.')
    parts=PurePosixPath(relative)
    if parts.is_absolute() or any(p in ('','..','.') for p in relative.split('/')):
        raise SafetyError('Sidecar path escapes its tile folder.')
    root=Path(folder).resolve();path=root.joinpath(*parts.parts)
    current=root
    for part in parts.parts:
        current=current/part
        if current.is_symlink() or (current.exists() and getattr(current.stat(),'st_file_attributes',0)&0x400):
            raise SafetyError('Sidecar path is redirected.')
    if not path.resolve().is_relative_to(root):raise SafetyError('Sidecar path escapes its tile folder.')
    return path


def _members(hashes,tile_name=None):
    names=[re.fullmatch(r'tiles/(tile_\d{6})\.json',p) for p in hashes]
    names=[m.group(1) for m in names if m]
    if len(names)!=1 or (tile_name is not None and names[0]!=tile_name):
        raise SafetyError('Sidecar backup requires one manifest tile identity.')
    name=names[0]
    expected=[f'tiles/{name}.json',f'tiles/{name}.flakes.json',f'boundary-labels/{name}.npz']
    if any(p not in hashes for p in expected):raise SafetyError('Manifest lacks required sidecar hashes.')
    archive=f'tiles/{name}.zen-vendor.json'
    if archive in hashes:expected.append(archive)
    if any(not isinstance(hashes[p],str) or not re.fullmatch('[0-9a-f]{64}',hashes[p]) for p in expected):
        raise SafetyError('Invalid sidecar manifest digest.')
    return name,expected


def create_tile_sidecar_backup(folder,tile_name,hashes):
    """Persist a content-addressed bundle before committing its manifest row.

    A crash before the row commit can leave an orphan. A recapture with new
    sidecars receives a distinct name; neither orphan nor committed bundle is
    replaced. Deterministic ZIP headers let identical retries reuse one bundle.
    """
    name,members=_members(hashes,tile_name);payloads={};total=0
    for relative in members:
        path=_path(folder,relative)
        if path.stat().st_size>MAX_BUNDLE_BYTES-total:raise SafetyError('Sidecar bundle exceeds bounded size.')
        data=path.read_bytes();total+=len(data)
        if total>MAX_BUNDLE_BYTES or _digest(data)!=hashes[relative]:raise SafetyError('Original sidecar differs from manifest.')
        payloads[relative]=data
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for relative,data in payloads.items():
            entry=zipfile.ZipInfo(relative,date_time=(1980,1,1,0,0,0))
            entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,data)
    blob=output.getvalue()
    if len(blob)>MAX_BUNDLE_BYTES:raise SafetyError('Sidecar recovery bundle exceeds bounded size.')
    digest=_digest(blob)
    relative=f'tile-evidence/{name}.{digest}.zip';destination=_path(folder,relative)
    destination.parent.mkdir(parents=True,exist_ok=True)
    reference={'path':relative,'sha256':digest,'bytes':len(blob),
               'kind':'exact_original_sidecars_v1','member_count':len(members)}
    if destination.exists():
        # A repeat must preserve and verify the prior bundle, never replace it.
        if destination.stat().st_size>MAX_BUNDLE_BYTES:raise SafetyError('Existing sidecar bundle exceeds bounded size.')
        inspect_tile_sidecar_backup(folder,{'hashes':hashes,'sidecar_backup':reference})
        return reference
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.ff-sidecar-',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name);stream.write(blob);stream.flush();os.fsync(stream.fileno())
        temporary.replace(destination)
        with destination.open('rb+') as stream:os.fsync(stream.fileno())
        inspect_tile_sidecar_backup(folder,{'hashes':hashes,'sidecar_backup':reference})
        return reference
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()


def inspect_tile_sidecar_backup(folder,row):
    """Return verified exact member bytes without writing; old rows return {}.

    Verify every existing sidecar before returning any recovery evidence. A
    changed existing file is a conflict, even if the bundle remains correct.
    """
    reference=row.get('sidecar_backup')
    if reference is None:return {}
    hashes=row.get('hashes',{});name,members=_members(hashes)
    if (reference.get('kind')!='exact_original_sidecars_v1'
            or not isinstance(reference.get('sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',reference['sha256'])
            or reference.get('path')!=f'tile-evidence/{name}.{reference["sha256"]}.zip'
            or type(reference.get('bytes')) is not int or not 0<reference['bytes']<=MAX_BUNDLE_BYTES):
        raise SafetyError('Invalid sidecar bundle reference.')
    path=_path(folder,reference['path'])
    if path.stat().st_size!=reference['bytes']:raise SafetyError('Sidecar bundle length differs from manifest.')
    blob=path.read_bytes()
    if _digest(blob)!=reference.get('sha256'):raise SafetyError('Sidecar bundle hash differs from manifest.')
    payloads={}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries=archive.infolist()
            if len(entries)!=len(members) or sorted(e.filename for e in entries)!=sorted(members):
                raise SafetyError('Sidecar bundle members differ from manifest.')
            if sum(e.file_size for e in entries)>MAX_BUNDLE_BYTES:raise SafetyError('Expanded sidecar bundle exceeds bounded size.')
            for entry in entries:
                _path(folder,entry.filename)
                data=archive.read(entry)
                if _digest(data)!=hashes[entry.filename]:raise SafetyError('Sidecar bundle member differs from manifest.')
                payloads[entry.filename]=data
    except (zipfile.BadZipFile,RuntimeError,NotImplementedError) as exc:
        raise SafetyError('Unreadable sidecar recovery bundle.') from exc
    for relative in members:
        original=_path(folder,relative)
        if original.exists() and (original.stat().st_size>MAX_BUNDLE_BYTES or _digest(original.read_bytes())!=hashes[relative]):
            raise SafetyError('Existing sidecar changed; exact-byte recovery cannot overwrite it: '+relative)
    return payloads


def restore_tile_sidecars(folder,row):
    """Explicitly restore only missing original paths after validating all bytes."""
    payloads=inspect_tile_sidecar_backup(folder,row);restored=[]
    for relative,data in payloads.items():
        target=_path(folder,relative)
        if target.exists():continue
        target.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive creation refuses a file that appeared since validation.
        with target.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if _digest(target.read_bytes())!=row['hashes'][relative]:raise SafetyError('Restored sidecar did not retain its exact bytes.')
        restored.append(relative)
    return restored
