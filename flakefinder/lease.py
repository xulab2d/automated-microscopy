"""Prevent two FlakeFinder processes in this installation controlling one endpoint."""
import hashlib,os
from pathlib import Path
from .model import SafetyError

class HardwareLease:
    def __init__(self,endpoint,directory):
        folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
        self.path=folder/(hashlib.sha256(endpoint.encode()).hexdigest()[:16]+'.lock')
        self.file=None
    def acquire(self):
        if self.file is not None:return
        file=open(self.path,'a+b')
        if os.fstat(file.fileno()).st_size==0:file.write(b'0');file.flush()
        file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,BlockingIOError) as e:
            file.close();raise SafetyError('Another FlakeFinder process owns microscope acquisition. Stop or close that client first.') from e
        self.file=file
    def close(self):
        if self.file is not None:self.file.close();self.file=None
