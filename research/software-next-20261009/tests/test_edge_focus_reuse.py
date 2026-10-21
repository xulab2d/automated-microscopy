"""Saved fixed-ROI focus evidence, without real hardware."""
from dataclasses import asdict
from pathlib import Path
import json,os
import numpy as np
import pytest
from flakefinder.edge_focus_reuse import discovery_edge_focus_entries
from flakefinder.full_chip import load_seed_focus_points,_camera_context
from flakefinder.model import Frame,Position,CameraTransform,Limits,SafetyError,atomic_json
from flakefinder.imaging import save_frame
from flakefinder.chip_discovery import file_hash

@pytest.fixture
def saved_edge(tmp_path):
    root=tmp_path/'discovery';folder=root/'edge-focus'/'attempt_000'
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    selection={'box_px':[60,40,80,60],'point_xy_um':[70.,50.]}
    metadata={'camera_job_template':'Camera','camera_resources':{'exposure':200},'objective_position':2,
        'illumination_percent':90,'focus_only_roi_source_px':selection['box_px'],
        'physical_stage_position':{'x':50.,'y':50.,'z':10.},'focus_mask_policy':'include_chip_edge',
        'edge_focus_selection':selection}
    frame=Frame(np.full((20,20,3),1000,np.uint16),Position(70,50,10),(1,1),0,metadata=metadata)
    save_frame(folder,'verified_focus',frame)
    result={'success':True,'outcome':'resolved_peak','z_um':10.,'score':1.,
            'samples':[{'z_um':10.,'phase':'verification_confirmation','score':1.}]}
    atomic_json(folder/'focus-result.json',result);atomic_json(folder/'selection'/'selection.json',selection)
    discovery={'transform':asdict(transform),'source_shape':[100,100,3],
        'edge_focus_attempts':[{'state':'focused','folder':'edge-focus/attempt_000',
            'requested_xy_um':[50.,50.],'selection':selection,'result':result,
            'result_sha256':file_hash(folder/'focus-result.json')} ]}
    atomic_json(root/'discovery.json',discovery)
    plan={'discovery_folder':str(root),'discovery_sha256':file_hash(root/'discovery.json'),
          'transform':asdict(transform),'camera_shape':[100,100,3],'acquisition_settings':_camera_context(frame),
          'scan_plan':{'polygon_um':[[0,0],[100,0],[100,100],[0,100]]}}
    return root,folder,plan,Limits(0,100,0,100,0,50)


def test_registered_crop_point_is_reused_not_camera_centre(saved_edge):
    root,folder,plan,limits=saved_edge
    entries=discovery_edge_focus_entries(root)
    points,provenance=load_seed_focus_points(entries,plan,limits=limits)
    assert points==[[70.,50.,10.]]
    row=provenance['records'][0]
    assert row['kind']=='verified_edge_roi' and row['physical_stage_position_um']==[50.,50.,10.]
    assert row['local_focus_verified'] and row['xy_source']=='registered_fixed_edge_roi_in_verified_crop'
    assert load_seed_focus_points(provenance['records'],plan,limits=limits)[0]==points
    # Untyped cropped/full-frame pairs still cannot bypass the old strict path.
    with pytest.raises(SafetyError,match='camera geometry'):
        load_seed_focus_points([{'result':row['result'],'frame':row['frame']}],plan,limits=limits)


@pytest.mark.parametrize('change', ['hash','physical_xy','shape','profile','different_chip','unconfirmed'])
def test_bad_roi_evidence_is_rejected(saved_edge,change):
    root,folder,plan,limits=saved_edge;entries=discovery_edge_focus_entries(root)
    if change=='hash':entries[0]['frame_sha256']='0'*64
    elif change=='different_chip':plan['discovery_folder']=str(root/'other')
    elif change=='profile':plan['acquisition_settings']['objective_position']=3
    elif change in ('physical_xy','shape'):
        path=folder/'verified_focus.json';metadata=json.loads(path.read_text())
        if change=='physical_xy':metadata['metadata']['physical_stage_position']['x']=55.
        else:metadata['metadata']['focus_only_roi_source_px']=[60,40,90,60]
        atomic_json(path,metadata);entries=discovery_edge_focus_entries(root)
    else:
        source=root/'discovery.json';d=json.loads(source.read_text());result=d['edge_focus_attempts'][0]['result']
        result['samples']=[];atomic_json(folder/'focus-result.json',result)
        d['edge_focus_attempts'][0]['result_sha256']=file_hash(folder/'focus-result.json');atomic_json(source,d)
        plan['discovery_sha256']=file_hash(source);entries=discovery_edge_focus_entries(root)
    with pytest.raises(SafetyError):load_seed_focus_points(entries,plan,limits=limits)


def test_actual_saved_discovery_focus_reuses_roi_xyz(tmp_path,monkeypatch):
    run=os.environ.get('FLAKEFINDER_FOCUS_REPLAY')
    if not run:pytest.skip('No saved lab replay requested')
    run=Path(run);root=run/'discovery'/'candidate001'
    plan=json.loads((run/'chip001'/'coverage-plan.json').read_text())
    entries=discovery_edge_focus_entries(root)
    points,provenance=load_seed_focus_points(entries,plan)
    assert len(points)==2
    assert points[0]==pytest.approx([50674.53164269223,23558.438552578245,15627.422])
    assert points[1]==pytest.approx([47384.5745764311,21725.6216165981,15622.11])
    assert all(r['local_focus_verified'] for r in provenance['records'])
    assert np.linalg.norm(np.asarray(points[0][:2])-provenance['records'][0]['physical_stage_position_um'][:2])>50

    # Exercise the actual surface orchestration with synthetic focus responses
    # at only the missing planned anchors. This is never an adapter connection.
    import flakefinder.focus_surface as module
    from flakefinder.autofocus import FocusResult
    anchors=np.asarray(plan['anchor_xy_um'],float);matrix=np.asarray(plan['transform']['matrix'])
    h,w=plan['camera_shape'][:2]
    separation=np.linalg.norm(anchors[:,None,:]-anchors[None,:,:],axis=2);separation[separation==0]=np.inf
    tolerance=min(max(np.linalg.norm(matrix@v) for v in ([w/2,h/2],[w/2,-h/2])),separation.min()/3)
    coefficient=(points[0][2]-points[1][2])/(points[0][0]-points[1][0])
    height=lambda x:points[0][2]+coefficient*(x-points[0][0])
    class SimulatedStage:
        armed=True;limits=Limits(-90000,90000,-90000,90000,-25000,25000,3000,10)
        def __init__(self):self.p=Position(*points[0]);self.travels=[]
        def position(self):return self.p
        def check_cancel(self):pass
        def travel(self,x,y):self.p=Position(x,y,self.p.z);self.travels.append([x,y])
        def move_z(self,z):self.p=Position(self.p.x,self.p.y,z)
        def capture(self):return Frame(np.ones((20,20,3),np.uint16),self.p,(.1725,.1725),0)
    calls=[]
    def focus(scope,*args,**kwargs):
        p=scope.position();scope.scope.p=Position(p.x,p.y,height(p.x));calls.append([p.x,p.y])
        return FocusResult(True,height(p.x),1.,.5,'Synthetic test confirmation',[])
    monkeypatch.setattr(module,'autofocus',focus);monkeypatch.setattr(module,'quick_autofocus',focus)
    surface,verified=module.prepare_focus_surface(SimulatedStage(),anchors,
        {'half_range_um':4,'coarse_step_um':2,'fine_step_um':.5,'seed_match_tolerance_um':tolerance},tmp_path,seed_points=points)
    assert surface is not None and len(verified)==4 and len(calls)==2
    assert all(point in verified for point in points)
    distances=np.linalg.norm(np.asarray(points)[:,None,:2]-anchors[None,:,:],axis=2)
    expected=anchors[np.min(distances,axis=0)>tolerance]
    assert set(map(tuple,calls))==set(map(tuple,expected))
    atomic_json(tmp_path/'actual-edge-focus-reuse-replay.json',{'qualified_saved_roi_xyz_um':points,
        'anchor_match_tolerance_um':float(tolerance),'remaining_anchor_xy_um':calls,
        'new_simulated_focus_checks':len(calls),'verified_point_count':len(verified),'hardware_access':False})
