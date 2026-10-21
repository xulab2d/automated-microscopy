from types import SimpleNamespace
from dataclasses import asdict
import json
import pytest
from flakefinder.scan import ScanPlan,run_scan
from flakefinder.simulator import SimulatedMicroscope
from flakefinder.model import Limits,CameraTransform,AcquisitionError
from flakefinder.autofocus import FocusResult

@pytest.mark.parametrize("surface_ok",[True,False])
def test_scan_applies_plane_or_focuses_every_tile(tmp_path,monkeypatch,surface_ok):
    s=SimulatedMicroscope();s.arm(Limits(-100,2400,-100,2400,60,150,1200,25))
    class Surface:
        def predict(self,x,y):return 100+.002*x+.001*y
    monkeypatch.setattr("flakefinder.focus_surface.prepare_focus_surface",lambda *a,**k:(Surface() if surface_ok else None,[[0,0,100],[0,1000,101],[1000,0,102]]))
    calls=[]
    def focus(scope,*args,**kwargs):
        calls.append(scope.position());scope.move_z(100)
        return FocusResult(True,100,1,1,"Focused",[])
    monkeypatch.setattr("flakefinder.scan.autofocus",focus)
    plan=ScanPlan([0,1000,0,900],mode="survey",focus_strategy="surface",autofocus_every=99)
    state=run_scan(s,plan,CameraTransform([[1,0],[0,1]],(1,1),True),tmp_path)
    assert state["state"]=="complete"
    assert len(calls)==(0 if surface_ok else state["tile_count"])
    for item in state["completed"].values():
        if surface_ok:
            pos=item["position"]
            assert pos["z"]==pytest.approx(100+.002*pos["x"]+.001*pos["y"])
            assert item["focus_method"]=="measured_surface"
        else:assert item["focus_method"]=="local_autofocus"
