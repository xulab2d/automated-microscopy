"""Arbitrary-count, irregular-chip and gap regressions with synthetic images."""
from dataclasses import asdict
import json,math,subprocess,sys
from pathlib import Path
import cv2,numpy as np,pytest

from flakefinder.chip_discovery import BoundaryOptions,EvidenceMap,discover_seed_enclosure,discover_substrate_seeds,full_enclosure_scan_plan,plan_unknown_count_search,contained_field_size
from flakefinder.model import Frame,Position,Limits,CameraTransform,Cancelled,SafetyError,AcquisitionError


class ImageStage:
    """Calibrated synthetic substrate/holder images; zero real hardware imports."""
    def __init__(self,polygons,shape=(128,160),matrix=None):
        self.polygons=[np.asarray(p,np.float32) for p in polygons];self.shape=shape
        self.matrix=np.eye(2) if matrix is None else np.asarray(matrix)
        self.transform=CameraTransform(self.matrix.tolist(),(1.,1.),True)
        self.p=Position(500,500,100);self.sequence=0;self.moves=[];self.stopped=False;self.armed=True
        self.limits=Limits(0,1600,0,1200,50,150,300,10)
    def position(self):return self.p
    def check_cancel(self):
        if self.stopped:raise Cancelled('test stop')
    def travel(self,x,y):
        self.check_cancel();self.limits.check(Position(x,y,self.p.z));self.moves.append([x,y]);self.p=Position(x,y,self.p.z)
    def stop(self):self.stopped=True;self.armed=False
    def capture(self):
        self.check_cancel();self.sequence+=1;h,w=self.shape;yy,xx=np.mgrid[:h,:w]
        offsets=np.stack([xx-(w-1)/2,yy-(h-1)/2],axis=-1)@self.matrix.T
        coords=offsets+[self.p.x,self.p.y];rgb=np.full((h,w,3),[400,400,400],np.uint16)
        # Rasterize directly in camera coordinates, including rotated cameras.
        for poly in self.polygons:
            pixels=(poly-[self.p.x,self.p.y])@np.linalg.inv(self.matrix).T+[(w-1)/2,(h-1)/2]
            mask=np.zeros((h,w),np.uint8);cv2.fillPoly(mask,[np.rint(pixels).astype(np.int32)],1)
            rgb[mask>0]=[1800,1500,1950]
        return Frame(rgb,self.p,(1.,1.),self.sequence,metadata={'white_level':4095,'source':'TEST','objective_position':2,'camera_resources':{'fixed':1}})


RECT=[[200,200],[800,200],[800,800],[200,800]]
IRREGULAR=[[160,180],[780,230],[840,550],[650,820],[260,750],[190,460]]


def run_boundary(tmp_path,polygons=None,options=None,**kwargs):
    scope=ImageStage(polygons or [RECT]);reference=scope.capture()
    options=options or BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=250)
    result=discover_seed_enclosure(scope,[500,500],scope.transform,reference,tmp_path,options,**kwargs)
    return scope,reference,result


@pytest.mark.parametrize('polygon',[RECT,IRREGULAR])
def test_measured_enclosure_covers_all_true_chip_vertices(tmp_path,polygon):
    scope,_,result=run_boundary(tmp_path,[polygon])
    assert result['state']=='complete'
    bounds=result['enclosure']['scan_bounds_um'];truth=np.array(polygon)
    assert bounds[0]<=truth[:,0].min() and bounds[1]>=truth[:,0].max()
    assert bounds[2]<=truth[:,1].min() and bounds[3]>=truth[:,1].max()
    plan=full_enclosure_scan_plan(result,mode='survey',focus_strategy='surface')
    assert plan.bounds_um==bounds
    assert result['coverage_kind']=='boundary_evidence_not_full_interior_acquisition'
    assert len(result['records'])<200


def test_narrow_observed_gap_does_not_merge_neighboring_chip(tmp_path):
    neighbor=[[835,200],[1300,200],[1300,800],[835,800]]
    _,_,result=run_boundary(tmp_path,[RECT,neighbor])
    assert result['state']=='complete'
    assert result['enclosure']['scan_bounds_um'][1]<835
    assert result['enclosure']['physical_chip_identity'].startswith('not established')


def test_chip_cut_by_search_boundary_is_unresolved(tmp_path):
    options=BoundaryOptions([100,700,100,900],cell_um=5,max_probes=150)
    _,_,result=run_boundary(tmp_path,options=options)
    assert result['state']=='unresolved'
    with pytest.raises(AcquisitionError):full_enclosure_scan_plan(result)


def test_probe_budget_resume_reuses_saved_evidence(tmp_path):
    scope,reference,first=run_boundary(tmp_path,options=BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=4))
    assert first['state']=='unresolved' and len(first['records'])==4
    saved=(tmp_path/'frames/probe_000000.tiff').read_bytes()
    result=discover_seed_enclosure(scope,[500,500],scope.transform,reference,tmp_path,
        BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=250),resume=True)
    assert result['state']=='complete' and scope.sequence==len(result['records'])+1
    assert (tmp_path/'frames/probe_000000.tiff').read_bytes()==saved


def test_stop_cancels_without_return_motion_and_can_resume(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    def callback(*_):(tmp_path/'STOP').write_text('stop')
    options=BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=250)
    with pytest.raises(Cancelled):discover_seed_enclosure(scope,[500,500],scope.transform,reference,tmp_path,options,callback=callback)
    assert len(scope.moves)==1 and scope.stopped
    assert json.loads((tmp_path/'discovery.json').read_text())['state']=='stopped'


def test_changed_optical_profile_or_saved_pixels_block_resume(tmp_path):
    scope,reference,_=run_boundary(tmp_path,options=BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=2))
    options=BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=250)
    reference.metadata['objective_position']=3
    with pytest.raises(SafetyError,match='differs'):discover_seed_enclosure(scope,[500,500],scope.transform,reference,tmp_path,options,resume=True)
    reference.metadata['objective_position']=2
    with (tmp_path/'frames/probe_000000.tiff').open('ab') as stream:stream.write(b'changed')
    with pytest.raises(AcquisitionError,match='evidence changed'):discover_seed_enclosure(scope,[500,500],scope.transform,reference,tmp_path,options,resume=True)


def test_rotated_camera_has_valid_registered_boundary(tmp_path):
    theta=.24;matrix=[[math.cos(theta),-math.sin(theta)],[math.sin(theta),math.cos(theta)]]
    scope=ImageStage([IRREGULAR],matrix=matrix);reference=scope.capture()
    result=discover_seed_enclosure(scope,[500,500],scope.transform,reference,tmp_path,
        BoundaryOptions([20,1400,20,1100],cell_um=5,max_probes=350))
    assert result['state']=='complete'
    bounds=result['enclosure']['scan_bounds_um'];vertices=np.array(IRREGULAR)
    assert bounds[0]<=vertices[:,0].min() and bounds[1]>=vertices[:,0].max()
    assert bounds[2]<=vertices[:,1].min() and bounds[3]>=vertices[:,1].max()


def test_unknown_evidence_and_conflicts_never_close_a_chip():
    evidence=EvidenceMap([0,100,0,100],5);evidence.flags[:]=evidence.OBSERVED
    evidence.flags[10,10]|=evidence.SUBSTRATE
    assert evidence.enclosure([52,52])['status']=='unresolved'
    evidence.flags[7:14,7:14]|=evidence.HOLDER
    evidence.flags[8:13,8:13]=evidence.OBSERVED|evidence.SUBSTRATE
    assert evidence.enclosure([52,52])['status']=='enclosed'
    evidence.flags[7,10]|=evidence.SUBSTRATE  # Contradictory collar pixel opens a path.
    assert evidence.enclosure([52,52])['status']=='unresolved'


def test_unknown_count_lattice_covers_declared_disk_centres_without_grid_prior():
    plan=plan_unknown_count_search([0,1400,0,900],300,40)
    assert plan['expected_chip_count'] is None
    points=np.asarray(plan['probe_centres_um']);rng=np.random.default_rng(4)
    centres=rng.uniform([150,150],[1250,750],size=(200,2))
    distances=np.linalg.norm(centres[:,None,:]-points[None,:,:],axis=2).min(axis=1)
    assert np.all(distances+20<=150+1e-8)


def test_seed_survey_has_unknown_chip_count_and_never_claims_full_coverage(tmp_path):
    scope=ImageStage([RECT]);reference=scope.capture()
    plan=plan_unknown_count_search([100,900,100,900],600,40)
    result=discover_substrate_seeds(scope,scope.transform,reference,plan,tmp_path)
    assert result['state'] in ('complete','complete_with_uncertainty')
    assert result['seeds'] and result['chip_count'] is None and result['all_chips_found'] is False
    assert len(result['records'])==plan['probe_count']


def test_plan_cli_needs_no_hardware_configuration(tmp_path):
    script=Path(__file__).resolve().parents[1]/'scripts/discover_chip_boundaries.py'
    output=tmp_path/'plan.json'
    subprocess.run([sys.executable,str(script),'plan-search','--bounds','0','1000','0','900',
        '--minimum-chip-diameter-um','300','--output',str(output)],check=True,capture_output=True,text=True)
    assert json.loads(output.read_text())['expected_chip_count'] is None
