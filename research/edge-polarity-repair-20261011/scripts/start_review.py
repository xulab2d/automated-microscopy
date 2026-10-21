"""Start the review and explicit-start scan console (Python3.11+)."""
from pathlib import Path
import argparse
import os
import sys
import time
import webbrowser

code_root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(code_root))
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--project-root',type=Path)
p.add_argument('--campaign',default='data/whole-chip-standardized-01')
p.add_argument('--port',type=int,default=8770)
p.add_argument('--no-browser',action='store_true')
a=p.parse_args()
root=a.project_root or (Path(os.environ['FLAKEFINDER_PROJECT_ROOT']) if os.environ.get('FLAKEFINDER_PROJECT_ROOT') else None)
if root is None:
    root=next((parent for parent in [code_root,*code_root.parents] if (parent/'.flakefinder-project.json').is_file()),None)
if root is None:
    p.error('Select the shared project with --project-root; no local data fallback is used.')
from flakefinder.suite_server import serve_suite
server,url=serve_suite(root,a.port,a.campaign)
print('Review: '+url,flush=True)
print('Scan console: '+url+'scan',flush=True)
print('Images and labels: '+str(root.resolve()),flush=True)
print('Startup opens no microscope connection. Scan controls require the installed microscope station and an explicit Start. Close with Ctrl+C.',flush=True)
if not a.no_browser:webbrowser.open(url)
try:
    while True:time.sleep(1)
except KeyboardInterrupt:
    server.shutdown();server.server_close()
