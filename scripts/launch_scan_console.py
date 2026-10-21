"""Open the production scan UI. This launcher never starts acquisition."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser


def main(page='/'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    source = workspace/'research/software-next-20261009'
    policy = json.loads((workspace/'configs/storage.json').read_text(encoding='utf-8-sig'))
    project = Path(os.environ.get('FLAKEFINDER_PROJECT_ROOT', policy['project_root']))
    if not (project/'.flakefinder-project.json').is_file():
        raise RuntimeError('Mount the microscopy shared Drive before opening the scan console.')
    if page not in ('/','/scan','/review'):raise ValueError('Unknown console page')
    url = 'http://127.0.0.1:8770'+page
    try:
        with socket.create_connection(('127.0.0.1',8770), timeout=2):
            occupied = True
    except OSError:
        occupied = False
    if not occupied:
        logs = workspace/'research/scan-console-runtime'
        logs.mkdir(exist_ok=True)
        with (logs/'server.log').open('ab') as log:
            child = subprocess.Popen([sys.executable,'-m','flakefinder.suite_server',str(project),'--port','8770'],
                cwd=source, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        (logs/'server.pid').write_text(str(child.pid))
        for _ in range(60):
            if child.poll() is not None:
                raise RuntimeError('Console exited. See '+str(logs/'server.log'))
            try:
                with socket.create_connection(('127.0.0.1',8770), timeout=.5):break
            except OSError:time.sleep(.5)
    with urlopen('http://127.0.0.1:8770/scan', timeout=10) as response:
        if b'id="runCountsNote"' not in response.read():
            raise RuntimeError('Port 8770 is occupied by a different application.')
    if not args.no_browser:webbrowser.open(url)
    print('Flakefinder ready: '+url+' (no acquisition started)')


if __name__ == '__main__':
    main()
