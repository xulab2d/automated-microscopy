"""Host-wide endpoint exclusion, plus legacy checkout locks for old clients."""
import hashlib,os,tempfile
from pathlib import Path
from .model import SafetyError

def host_lease_directory():
    if os.name=='nt':
        base=os.environ.get('ProgramData')
        if not base:raise SafetyError('ProgramData is unavailable; cannot establish host-wide microscope exclusion.')
        return Path(base)/'FlakeFinder'/'hardware-leases'
    return Path(tempfile.gettempdir())/'flakefinder-hardware-leases'


def endpoint_names(endpoint):
    value=str(endpoint).strip()
    if not value:raise ValueError('Empty hardware endpoint.')
    host,separator,port=value.rpartition(':')
    if separator and port.isdigit() and host.lower() in ('localhost','127.0.0.1','[::1]'):
        return 'localhost:'+port,sorted({value,'localhost:'+port,'127.0.0.1:'+port,'[::1]:'+port})
    return value,[value]


class HardwareLease:
    def __init__(self,endpoint,directory,*,global_directory=None,legacy_directories=()):
        canonical,names=endpoint_names(endpoint)
        folder=Path(directory).resolve()
        global_folder=Path(global_directory) if global_directory is not None else host_lease_directory()
        name=lambda value:hashlib.sha256(value.encode()).hexdigest()[:16]+'.lock'
        self.path=global_folder/name(canonical)
        compatibility={folder,*[Path(p).resolve() for p in legacy_directories]}
        from .storage import load_policy
        policy=load_policy()
        if policy and policy.get('installation_root'):
            compatibility.add(Path(policy['installation_root']).resolve()/'data'/'locks')
        # A staged checkout under the original installation must exclude its
        # still-running old GUI, whose lock name predates host-wide ownership.
        for parent in folder.parents:
            if (parent/'flakefinder'/'zen.py').is_file():compatibility.add(parent/'data'/'locks')
        self.paths=[self.path]+sorted({p/name(n) for p in compatibility for n in names}-{self.path},key=str)
        self.file=None;self._files=[]
    def acquire(self):
        if self.file is not None:return
        try:
            for path in self.paths:
                path.parent.mkdir(parents=True,exist_ok=True)
                file=open(path,'a+b');self._files.append(file)
                if os.fstat(file.fileno()).st_size==0:file.write(b'0');file.flush()
                file.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError) as e:
            self.close();raise SafetyError('Microscope lease unavailable: another client owns it or the host/legacy lock directory is not writable. No local fallback.') from e
        self.file=self._files[0]
    def close(self):
        for file in reversed(self._files):file.close()
        self._files=[];self.file=None
