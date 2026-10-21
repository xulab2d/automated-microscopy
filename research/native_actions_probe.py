"""Read-only validation of native identity and renderer recognition; no actions."""
from pathlib import Path
import sys,json,ctypes
sys.path.insert(0,str(Path(__file__).parent/'software-next-20261009/scripts'))
from native_memory_actions import NativeActions
n=NativeActions();rows=n.candidates()
print(json.dumps({'current_sid':n.sid,'candidates':rows}))
