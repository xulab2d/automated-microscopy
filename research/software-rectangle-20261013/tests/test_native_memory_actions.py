import importlib.util,json
from pathlib import Path
from types import SimpleNamespace as N
import pytest
spec=importlib.util.spec_from_file_location('native_memory_actions',Path(__file__).parents[1]/'scripts/native_memory_actions.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.mark.parametrize('path,name,allowed',[
 (r'C:\Program Files\WindowsApps\Microsoft.YourPhone_1_x64__id\PhoneExperienceHost.exe','PhoneExperienceHost',True),
 (r'C:\Program Files\WindowsApps\Microsoft.WidgetsPlatformRuntime_1_x64__id\WidgetService\WidgetService.exe','WidgetService',True),
 (r'C:\Temp\PhoneExperienceHost.exe','PhoneExperienceHost',False),
 (r'C:\Program Files\Carl Zeiss\ZEN core\ZENcore.exe','ZENcore',False),
 (r'C:\Program Files\Google\Drive File Stream\132.0.0.0\GoogleDriveFS.exe','GoogleDriveFS',False),
 (r'C:\Windows\explorer.exe','explorer',False),
 (r'C:\Users\STM\Documents\AnyDesk.exe','AnyDesk',False),
 (r'C:\Program Files\Dell\DellOptimizer\Console\DellOptimizer.Systray.exe','DellOptimizer.Systray',True)])
def test_exact_allowlist(path,name,allowed):assert m.allowed_image(name,path)==allowed

def test_renderer_requires_installed_image_and_exact_process_type():
 p=r'C:\Program Files\WindowsApps\OpenAI.Codex_26.1002.7124.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe'
 assert m.renderer(p,'app --type=renderer --foo')
 assert not m.renderer(p,'app --type=gpu-process')
 assert not m.renderer(p,'app --type=renderer-fake')
 assert not m.renderer(r'C:\Temp\ChatGPT.exe','--type=renderer')

def test_legacy_cooldown_stays_utc_and_exact_identity():
 assert m.expiry('10/11/2026 20:03:15')==m.expiry('2026-10-11T20:03:15Z')
 row=dict(pid=1,ticks=5,path='installed',sid='same',name='ChatGPT')
 assert m.same(row,dict(row,ws=2))
 for key in row:assert not m.same(row,dict(row,**{key:'different'}))

def setup(tmp_path,rows,life=lambda:'running',available=lambda:1,now=lambda:100):
 run=tmp_path/'project/data/run';folder=run/'memory-guard';folder.mkdir(parents=True)
 (tmp_path/'project/scan-control').mkdir()
 def read(p):return json.loads(p.read_text())
 def write(p,v):p.write_text(json.dumps(v))
 write(folder/'status.json',{})
 calls=[]
 def act(row,before):before(row);calls.append(row);return {'state':'trimmed' if row['action']=='trim' else 'stopped'}
 native=N(candidates=lambda:rows,act=act)
 return m.Cleanup(run,N(available=available),life,read,write,native,now),calls

def rows(mode='trim',n=4):return [dict(pid=i,ticks=10+i,path='verified',sid='same',name='utility'+str(i),action=mode) for i in range(n)]

def test_max_three_persistent_cooldowns_and_no_repeat(tmp_path):
 c,calls=setup(tmp_path,rows());c.pass_once();assert len(calls)==3
 c.pass_once();assert len(calls)==4
 c.pass_once();assert len(calls)==4
 assert len(json.loads(c.ledger.read_text()))==4

def test_read_existing_future_cooldown_before_actions(tmp_path):
 c,calls=setup(tmp_path,rows(n=1));c.write(c.ledger,{'0:10':'2026-10-11T20:50:00Z'})
 c.pass_once();assert not calls

def test_worker_exit_or_adequate_memory_prevents_actions(tmp_path):
 c,calls=setup(tmp_path,rows(),life=lambda:'stopped');c.pass_once();assert not calls
 c.life=lambda:'running';c.host.available=lambda:3*1024**3;c.pass_once();assert not calls

def test_final_lifecycle_recheck_prevents_action(tmp_path):
 state=iter(['running','stopped'])
 c,calls=setup(tmp_path,rows(n=1),life=lambda:next(state));c.pass_once();assert not calls

def test_logging_failure_prevents_mutation(tmp_path):
 c,calls=setup(tmp_path,rows(n=1))
 c.event=lambda *a:(_ for _ in ()).throw(OSError('disk unavailable'))
 with pytest.raises(OSError):c.pass_once()
 assert not calls

def test_restarted_utility_has_name_cooldown(tmp_path):
 values=rows('stop',1);c,calls=setup(tmp_path,values);c.pass_once()
 values[0]['pid']=987;c.pass_once();assert len(calls)==1
