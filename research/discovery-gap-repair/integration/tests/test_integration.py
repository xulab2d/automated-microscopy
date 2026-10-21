"""Small isolated acquisition safety/resume tests. Zero hardware connections."""
from pathlib import Path
import hashlib,json,sys
import cv2,numpy as np,pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from overlay import HERE,ROOT,profile_for
sys.path.insert(0,str(ROOT/'tests'))
from test_chip_discovery import ImageStage,RECT
from review_ff.sparse_acquisition import discover_sparse_outline,SparseEdgeOptions,_gap_probe_direction,_complete_missing_edge_directions
from review_ff.sparse_edges import fit_validated_outline,validated_coherent_edge,coherent_edge
from review_ff.photometric_edges import spatial_bracket_evidence
from review_ff.model import SafetyError,Cancelled,AcquisitionError

cv2.setNumThreads(1)
class Stage(ImageStage):
    def capture(self):
        frame=super().capture()
        frame.metadata.update(objective={'magnification':20},camera_job_template='synthetic fixed 20x',illumination_percent=90)
        return frame

def setup():
    scope=Stage([RECT]);reference=scope.capture()
    options=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20)
    return scope,reference,options,profile_for(reference)

def run(scope,reference,options,profile,tmp,**kwargs):
    return discover_sparse_outline(scope,[500,500],scope.transform,reference,tmp,options,edge_validation_profile=profile,**kwargs)

def test_budget_resume_immutable_raw_and_callback(tmp_path):
    scope,ref,opts,profile=setup();opts.max_probes=4;events=[]
    first=run(scope,ref,opts,profile,tmp_path,callback=lambda *args:events.append(args[0]))
    assert len(first['records'])==4 and first['budget_exhausted'] and len(events)==4
    hashes={name:hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()for rec in first['records']for name in rec['hashes']}
    before=scope.sequence
    second=run(scope,ref,opts,profile,tmp_path,resume=True)
    assert len(second['records'])==4 and scope.sequence==before
    assert all(hashlib.sha256((tmp_path/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items())

def test_stop_authoritative_no_new_motion_and_callback_restored(tmp_path):
    scope,ref,opts,profile=setup();original=scope.check_cancel
    tmp_path.joinpath('STOP').write_text('operator stop')
    with pytest.raises(Cancelled):run(scope,ref,opts,profile,tmp_path,resume=True)
    assert scope.moves==[] and scope.sequence==1 and scope.check_cancel==original
    assert (tmp_path/'STOP').read_text()=='operator stop'

def test_unsupported_profile_no_motion(tmp_path):
    scope,ref,opts,profile=setup();profile['holder_model']='bright_metal'
    result=run(scope,ref,opts,profile,tmp_path)
    assert result['state']=='unresolved' and result['reason']=='unsupported_edge_profile'
    assert not scope.moves and not result['records']

def test_capture_profile_drift_aborts(tmp_path,monkeypatch):
    scope,ref,opts,profile=setup();capture=scope.capture
    def drift():
        frame=capture();frame.metadata['illumination_percent']=91;return frame
    monkeypatch.setattr(scope,'capture',drift)
    with pytest.raises(AcquisitionError,match='profile changed'):run(scope,ref,opts,profile,tmp_path)
    assert scope.stopped and not list((tmp_path/'frames').glob('*'))

def test_resume_rejects_profile_and_legacy_identity_even_source_flag(tmp_path):
    scope,ref,opts,profile=setup();opts.max_probes=1
    first=run(scope,ref,opts,profile,tmp_path);before=len(scope.moves)
    changed=dict(profile,profile_id='another measured context')
    with pytest.raises(SafetyError,match='differs'):run(scope,ref,opts,changed,tmp_path,resume=True,allow_source_change=True)
    destination=tmp_path/'discovery.json';saved=json.loads(destination.read_text())
    saved['algorithm']='sparse_coarse_bracket_halfplanes_v1';saved['fingerprint']='legacy'
    destination.write_text(json.dumps(saved))
    with pytest.raises(SafetyError,match='differs'):run(scope,ref,opts,profile,tmp_path,resume=True,allow_source_change=True)
    assert len(scope.moves)==before

def test_registered_contradiction_removes_polygon():
    edges=[]
    for normal,offset,points in [([1,0],800,[[800,200],[800,800]]),([-1,0],-200,[[200,200],[200,800]]),
            ([0,1],800,[[200,800],[800,800]]),([0,-1],-200,[[200,200],[800,200]])]:
        edges.append(dict(normal=normal,offset_um=offset,points_um=points,image='synthetic measured edge'))
    observed=[('independent substrate',np.ones((160,160),np.uint8),[1000,500])]
    result=fit_validated_outline(edges,[500,500],[0,1500,0,1200],20,observed,np.eye(2),[160,160,3])
    assert result['status']=='unresolved' and result['reason']=='registered_substrate_contradicts_halfplanes'
    assert result['polygon_um']==[] and len(result['lines'])==4

def test_spatial_unknown_and_disconnected_support_never_choose_side():
    empty=np.zeros((100,100),np.uint8)
    assert spatial_bracket_evidence(empty,'unknown')['side']=='unknown'
    split=np.ones((100,100),np.uint8);split[:,49:51]=2
    assert spatial_bracket_evidence(split,'unknown')['side']=='unknown'
    dirt=np.ones((100,100),np.uint8);dirt[48:53,48:53]=0
    assert spatial_bracket_evidence(dirt,'unknown')['side']=='substrate'

def test_gap_pass_uses_validated_fitter_and_budget_callbacks():
    # Rotated off-centre seed: E+N rays can share NE edge; missing normal is NW.
    normals=[[2**-.5,2**-.5],[2**-.5,-2**-.5],[-2**-.5,-2**-.5]]
    lines=[dict(normal=n,offset_um=500)for n in normals]
    request=_gap_probe_direction(lines,[0,0],[-1000,1000,-1000,1000])
    assert np.dot(request['direction'],[-2**-.5,2**-.5])>.99
    edges=[];moves=[];checks=[];saved=[];remaining=[1];fits=[]
    def fit_model():
        fits.append(True);return {'status':'estimated' if edges else 'unresolved','lines':lines}
    def acquire(xy,phase):
        moves.append(xy);remaining[0]-=1;edge={'normal':request['direction'],'offset_um':100,'points_um':[]};edges.append(edge)
        return {'edge':edge,'image':'synthetic measured edge','classification':'unknown'}
    result=_complete_missing_edge_directions(edges,[0,0],[-1000,1000,-1000,1000],[200,200],200,20,
        acquire,lambda r,d:r is not None and np.dot(r['edge']['normal'],d)>.15,lambda r:r['classification'],
        lambda:checks.append(True),lambda *args:saved.append(args),lambda:remaining[0],fit_model)
    assert result['status']=='estimated' and len(moves)==1 and checks and fits and remaining[0]==0


def test_rotated_offcentre_rectangle_measures_missing_side(tmp_path):
    # E/N hit the same NE side; the angular gap must acquire the missing SW side.
    scope=Stage([[[500,150],[850,500],[500,850],[150,500]]])
    scope.travel(620,550);ref=scope.capture();profile=profile_for(ref)
    opts=SparseEdgeOptions([20,1200,20,1100],coarse_step_um=300,cell_um=5,margin_um=20)
    result=discover_sparse_outline(scope,[620,550],scope.transform,ref,tmp_path,opts,edge_validation_profile=profile)
    assert result['state']=='estimated' and len(result['estimated_outline']['lines'])==4
    assert len(result['records'])<=32 and any(key.startswith('gap_')for key in result['rays'])
    assert result['estimated_outline']['registered_consistency']['accepted']


def test_bright_raw_holder_cannot_pass_declared_dark_model():
    scope,ref,_,profile=setup();scope.travel(200,500);frame=scope.capture()
    labels=np.where(frame.rgb[:,:,0]>1000,1,2).astype(np.uint8)
    assert coherent_edge(labels,[200,500],frame.rgb.shape,scope.matrix) is not None
    frame.rgb[:]=4095-frame.rgb
    edge,report=validated_coherent_edge(frame,labels,scope.matrix,profile)
    assert edge is None and not report['accepted']
