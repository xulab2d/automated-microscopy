"""Compatibility launcher for the consolidated FlakeFinder home."""
from pathlib import Path
import os
import sys
import traceback
from launch_scan_console import main

if __name__=='__main__':
    try:main('/')
    except Exception as exc:
        log=Path(__file__).resolve().parents[1]/'research/scan-console-runtime/review-launch-error.log'
        log.parent.mkdir(parents=True,exist_ok=True)
        log.write_text(traceback.format_exc(),encoding='utf-8')
        if os.name=='nt' and sys.stderr is None:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(exc)+'\n\nDetails: '+str(log),'FlakeFinder Review',0x10)
        raise
