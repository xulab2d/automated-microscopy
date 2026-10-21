"""Pure batch-state/cancellation regressions; never connect microscope APIs."""
from concurrent.futures import Future
import importlib.util,json,threading,time
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('batch_runner',Path(__file__).resolve().parents[1]/'scripts/scan_chip_batch.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

def saved_batch(tmp_path):
 config={'schema':1,'backend':'simulator','profile':'20x'}
 layout={'chips':[{'id':'A1','centre_um':[1000,1000],'status':'substrate_verified'}]}
 state={'patch_size_um':1200.,'chip_states':{'A1':{'state':'complete'}},
        'fingerprint':runner.batch_fingerprint(config,layout,1200.)}
 (tmp_path/'batch.json').write_text(json.dumps(state))
 return config,layout

@pytest.mark.parametrize('change',['config','layout','size'])
def test_resume_rejects_changed_acquisition_before_connection(tmp_path,change):
 config,layout=saved_batch(tmp_path);size=1200.
 if change=='config':config['profile']='50x'
 if change=='layout':layout['chips'][0]['centre_um'][0]+=100
 if change=='size':size=1800.
 with pytest.raises(ValueError,match='differs'):runner.validate_resume(tmp_path,config,layout,size)

def test_identical_resume_preserves_completed_chips(tmp_path):
 config,layout=saved_batch(tmp_path)
 assert runner.validate_resume(tmp_path,config,layout,1200.)['chip_states']['A1']['state']=='complete'

def test_analysis_observes_stop_before_image_work(tmp_path):
 cancel=tmp_path/'STOP';cancel.write_text('stop')
 with pytest.raises(runner.Cancelled):runner.analyse_one(str(tmp_path),{'backend':'empirical'},str(cancel))

def test_running_analysis_gets_cooperative_stop_and_is_waited_for(tmp_path):
 stop=tmp_path/'STOP';future=Future();future.set_running_or_notify_cancel()
 class Pool:
  def __init__(self):self.shutdown_called=False
  def shutdown(self,**kwargs):self.shutdown_called=True;assert future.done()
 pool=Pool()
 def worker():
  deadline=time.monotonic()+2
  while not stop.exists() and time.monotonic()<deadline:time.sleep(.005)
  future.set_exception(runner.Cancelled('stopped'))
 thread=threading.Thread(target=worker);thread.start()
 runner.stop_analysis(pool,{'A1':future},stop,timeout=2);thread.join()
 assert pool.shutdown_called and stop.exists()

def test_connection_failure_records_failed_state(tmp_path,monkeypatch):
 config={'schema':1,'backend':'simulator'}
 layout={'chips':[{'id':'A1','centre_um':[1000,1000],'status':'substrate_verified'}]}
 config_path=tmp_path/'config.json';config_path.write_text(json.dumps(config))
 layout_path=tmp_path/'layout.json';layout_path.write_text(json.dumps(layout))
 output=tmp_path/'batch'
 monkeypatch.setattr(runner.sys,'argv',['scan_chip_batch','--config',str(config_path),'--layout',str(layout_path),'--output',str(output),'--patch-size-um','1200'])
 def unavailable(_):raise runner.AcquisitionError('connection unavailable')
 monkeypatch.setattr(runner,'connect',unavailable)
 with pytest.raises(runner.AcquisitionError,match='connection unavailable'):runner.main()
 state=json.loads((output/'batch.json').read_text())
 assert state['state']=='failed' and 'connection unavailable' in state['error']
