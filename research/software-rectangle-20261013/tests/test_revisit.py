from dataclasses import asdict
from types import SimpleNamespace as N
import json
import cv2
import numpy as np
import pytest
from flakefinder.model import Frame,Position,CameraTransform,SafetyError,AcquisitionError,atomic_json
from flakefinder.revisit import register_candidate,build_queue,run_revisits
from flakefinder.optics import optimize_exposure
from flakefinder.imaging import save_frame
from test_integration import bare_zen

def texture(seed=2):
    rng=np.random.default_rng(seed);a=np.full((600,800,3),1800,np.float32)
    for _ in range(900):
        point=tuple(map(int,rng.integers([10,10],[790,590])))
        cv2.circle(a,point,int(rng.integers(2,9)),tuple(map(float,rng.integers(700,2800,3))),-1)
    return np.uint16(cv2.GaussianBlur(a,(0,0),.7))

def test_registration_locates_candidate_at_three_times_magnification():
    low=Frame(texture(),Position(0,0,0),(1,1),0)
    affine=np.float32([[3,0,-800],[0,3,-600]])
    high=Frame(cv2.warpAffine(low.rgb,affine,(800,600)),Position(0,0,0),(1/3,1/3),1)
    source=CameraTransform([[1,0],[0,1]],(1,1),True)
    target=CameraTransform([[1/3,0],[0,1/3]],(1/3,1/3),True)
    result=register_candidate(low,high,source,target,[400,300])
    assert np.linalg.norm(np.array(result['candidate_px'])-[400,300])<2
    assert result['inliers']>=8 and result['relative_transform_error']<.02
    with pytest.raises(AcquisitionError):register_candidate(low,high,source,source,[400,300])
    unrelated=Frame(texture(9),Position(0,0,0),(1,1),2)
    with pytest.raises(AcquisitionError):register_candidate(low,unrelated,source,source,[400,300])

def test_queue_respects_reviews_and_samples_multiple_tiles(tmp_path):
    rows=[]
    for i in range(3):
        name=f'tile_{i:06d}'
        save_frame(tmp_path/'tiles',name,Frame(texture(i),Position(i*1000,0,0),(1,1),i))
        for j in range(3):rows.append({'id':f'{i}-{j}','tile_id':name,'source_image':f'tiles/{name}.tiff',
              'priority':'low_contrast','disposition':'candidate','quality_score':1-i*.1-j*.01})
    atomic_json(tmp_path/'flakes.json',{'dataset_id':'a','source_folder':str(tmp_path),'flakes':rows})
    atomic_json(tmp_path/'reviews.json',{'dataset_id':'a','annotations':{'0-0':{'decision':'reject'},'1-0':{'decision':'keep'}}})
    queue=build_queue(tmp_path,max_candidates=3)
    assert len({r['tile_id'] for r in queue['candidates']})==3
    assert '0-0' not in [r['id'] for r in queue['candidates']]
    assert [r['id'] for r in build_queue(tmp_path,'kept')['candidates']]==['1-0']
    scope=N(armed=True,hardware_controls=lambda:N(objectives=lambda:{'position':1}))
    profile={'camera_transform':asdict(CameraTransform([[.2,0],[0,.2]],(.2,.2),True)),
             'objective':{'position':3,'name':'50x'}}
    with pytest.raises(SafetyError,match='Engage objective'):run_revisits(scope,tmp_path,profile,tmp_path/'output')
    assert not (tmp_path/'output').exists()

def test_exposure_optimizer_converges_and_saves_measured_setting(tmp_path):
    class Camera:
        armed=True
        def check_cancel(self):pass
        def set_exposure_ms(self,value):self.exposure=value
        def capture(self):
            return Frame(np.full((80,100,3),round(self.exposure*10),np.uint16),Position(0,0,0),(1,1),0)
    camera=Camera();result=optimize_exposure(camera,tmp_path,initial_ms=100)
    assert result['converged']
    assert camera.exposure==result['steps'][-1]['exposure_ms']==result['exposure_ms']
    assert abs(result['steps'][-1]['rgb_q95'][0]/4095-.72)<.02

def test_material_selection_gates_automatic_revisits_but_preserves_reviewed_keeps(tmp_path):
    image=tmp_path/'tile.tiff';image.write_bytes(b'queue does not decode the image')
    base={'tile_id':'tile','source_image':image.name,'priority':'review_required','disposition':'candidate'}
    rows=[{**base,'id':key,'material_selection':{'status':status,'catalog_selected':selected}}
          for key,status,selected in [('selected','material_like',True),('specks','material_like',False),
                                      ('dust','excluded',False),('unknown','uncertain',False)]]
    atomic_json(tmp_path/'flakes.json',{'dataset_id':'material','source_folder':str(tmp_path),'flakes':rows})
    assert [r['id'] for r in build_queue(tmp_path)['candidates']]==['selected']
    atomic_json(tmp_path/'reviews.json',{'dataset_id':'material','annotations':{'unknown':{'decision':'keep'}}})
    assert [r['id'] for r in build_queue(tmp_path,'kept')['candidates']]==['unknown']
    assert {r['id'] for r in build_queue(tmp_path,'all')['candidates']}=={'selected','unknown'}

def test_quarter_micron_focus_step_is_not_skipped():
    scope=bare_zen();position=[Position(10000,20000,3000)];sent=[]
    scope.position=lambda:position[0]
    def move(stub,request):
        sent.append(request.value);position[0]=Position(10000,20000,request.value*1e6)
    scope._motion_rpc=move
    scope.move_z(3000.25)
    assert len(sent)==1 and position[0].z==pytest.approx(3000.25)

def test_checkpoint_survives_transient_windows_sharing_error(tmp_path,monkeypatch):
    from pathlib import Path
    checkpoint=tmp_path/'checkpoint.json';atomic_json(checkpoint,{'completed':1})
    replace=Path.replace;attempts=[]
    def busy_once(self,target):
        attempts.append(1)
        if len(attempts)==1:
            assert json.loads(checkpoint.read_text())['completed']==1
            raise PermissionError('Temporary sharing violation')
        return replace(self,target)
    monkeypatch.setattr(Path,'replace',busy_once)
    atomic_json(checkpoint,{'completed':2})
    assert len(attempts)==2 and json.loads(checkpoint.read_text())['completed']==2
    assert not list(tmp_path.glob('*.tmp'))
