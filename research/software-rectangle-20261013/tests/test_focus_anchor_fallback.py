"""Adapter integration: alternate ROI proposal never changes raw focus data."""
from types import SimpleNamespace

import numpy as np
import pytest

from flakefinder import full_chip
from flakefinder.model import AcquisitionError, CameraTransform, Frame, Position, SafetyError


def field(uniform=False, exposure=200.):
    rgb=np.full((256,256,3),120,np.uint16)
    if not uniform:
        rgb[:,:128]=[1400,1800,2200]
    return Frame(rgb,Position(10,20,30),(1.,1.),1,
                 metadata={'camera_resources':{'ff_exposure_ms':exposure}})


def test_primary_registration_failure_uses_same_field_fallback_and_keeps_raw_roi(monkeypatch):
    import flakefinder.autonomous_reference as bootstrap
    full=field(); captures=[]; proposals=[]
    def capture():
        captures.append(1)
        return full
    def reject(*args,**kwargs):
        raise AcquisitionError('Busy texture defeats primary proposal.')
    actual_selector=bootstrap.select_edge_roi
    def fallback(frame,roi_um,transform):
        proposals.append(frame)
        return actual_selector(frame,roi_um,transform)
    monkeypatch.setattr(full_chip,'edge_focus_roi',reject)
    monkeypatch.setattr(bootstrap,'select_edge_roi',fallback)
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    adapter=full_chip.FocusROIAdapter(SimpleNamespace(capture=capture),64,transform,reference_frame=full,edge=True)
    before=full.rgb.copy()
    adapter.begin_focus_anchor()
    assert captures==[1] and proposals==[full]
    assert adapter.selection_frame is full
    assert 'primary_selector_rejection' in adapter.selection
    box=list(adapter.box)
    crop=adapter.capture()
    x0,y0,x1,y1=box
    assert crop.rgb.shape==(64,64,3)
    assert np.array_equal(crop.rgb,full.rgb[y0:y1,x0:x1])
    assert np.array_equal(full.rgb,before)
    assert adapter.box==box
    assert crop.metadata['focus_mask_policy']=='include_chip_edge'
    assert crop.metadata['physical_stage_position']=={'x':10,'y':20,'z':30}


def test_uniform_anchor_still_fails_without_a_focus_proposal():
    full=field(uniform=True); captures=[]
    def capture():
        captures.append(1)
        return full
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    adapter=full_chip.FocusROIAdapter(SimpleNamespace(capture=capture),64,transform,reference_frame=full,edge=True)
    with pytest.raises(AcquisitionError):
        adapter.begin_focus_anchor()
    assert captures==[1]
    assert adapter.box is None and adapter.selection['status']=='failed'


def test_camera_profile_failure_does_not_use_fallback(monkeypatch):
    import flakefinder.autonomous_reference as bootstrap
    def forbidden(*args,**kwargs):
        pytest.fail('Optical profile change must fail before ROI fallback.')
    monkeypatch.setattr(bootstrap,'select_edge_roi',forbidden)
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    adapter=full_chip.FocusROIAdapter(SimpleNamespace(capture=lambda:field(exposure=300.)),64,
        transform,reference_frame=field(),edge=True)
    with pytest.raises(SafetyError,match='Optical profile changed'):
        adapter.begin_focus_anchor()


def test_measured_anchor_prior_guides_current_raw_transition(monkeypatch):
    full=field(); transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    original=full_chip.edge_focus_roi; calls=[]
    def busy(frame,roi_um,camera_transform,*,boundary_proposal=None):
        calls.append(boundary_proposal)
        if boundary_proposal is None:
            raise AcquisitionError('Busy image; global orientation unresolved.')
        return original(frame,roi_um,camera_transform,boundary_proposal=boundary_proposal)
    monkeypatch.setattr(full_chip,'edge_focus_roi',busy)
    proposal={'xy_um':[10,20],'normal':[1,0],'offset_um':10.,'source_image':'edge.tiff','source_hashes':{'edge.tiff':'f'*64}}
    adapter=full_chip.FocusROIAdapter(SimpleNamespace(capture=lambda:full),64,transform,
        reference_frame=full,edge=True,edge_proposals=[proposal])
    adapter.begin_focus_anchor()
    assert calls==[None,{'normal':[1,0],'offset_um':10.}]
    assert adapter.selection['method']=='label_guided_green_transition'
    assert adapter.selection['measured_anchor_proposal']['source_image']=='edge.tiff'
    assert adapter.selection['local_focus_verified'] is False
    crop=adapter.capture();x0,y0,x1,y1=adapter.box
    assert np.array_equal(crop.rgb,full.rgb[y0:y1,x0:x1])


def test_measured_prior_cannot_rescue_uniform_current_field():
    full=field(uniform=True); transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    adapter=full_chip.FocusROIAdapter(SimpleNamespace(capture=lambda:full),64,transform,
        reference_frame=full,edge=True,edge_proposals=[{'xy_um':[10,20],'normal':[1,0],'offset_um':10.}])
    with pytest.raises(AcquisitionError):adapter.begin_focus_anchor()
    assert adapter.box is None


def rectangle_discovery(tmp_path):
    records=[]
    definitions=[([[40,80],[50,80],[60,80]],[0,1],80),
                 ([[80,40],[80,50],[80,60]],[1,0],80),
                 ([[40,20],[50,20],[60,20]],[0,-1],-20),
                 ([[20,40],[20,50],[20,60]],[-1,0],-20)]
    for i,(points,normal,offset) in enumerate(definitions):
        image='edge%d.tiff'%i
        (tmp_path/image).write_bytes(b'immutable raw source')
        records.append({'image':image,'hashes':{image:full_chip.file_hash(tmp_path/image)},
            'position_um':[50,50,30],
            'edge':{'points_um':points,'normal':normal,'offset_um':offset}})
    derived=records[1].pop('edge')
    derived['image']=records[1]['image']
    model='f'*64
    discovery={'seed_xy_um':[50,50],'records':records,'rectangle_model_sha256':model,
        'rectangle_reanalysis':{records[1]['image']:{'edge':derived,'validation':{'accepted':True},
            'source_hashes':dict(records[1]['hashes']),'rectangle_model_sha256':model,
            'new_capture':False,'new_focus':False}}}
    return discovery


def test_rectangle_reanalysis_supplies_measured_fourth_anchor_without_mutating_original(tmp_path):
    import copy
    discovery=rectangle_discovery(tmp_path)
    original=copy.deepcopy(discovery)
    points,proof=full_chip.observed_edge_anchors(discovery,tmp_path,[[0,0],[100,0],[100,100],[0,100]])
    assert {tuple(p) for p in points}=={(50,80),(80,50),(50,20),(20,50)}
    anchor=next(p for p in proof['anchors'] if p['source_image']=='edge1.tiff')
    assert anchor['rectangle_model_sha256']=='f'*64
    assert anchor['original_capture_position_um']==[50,50,30]
    assert proof['local_focus_verified'] is False
    assert discovery==original


@pytest.mark.parametrize('change',['model','hash','rejected'])
def test_rectangle_anchor_requires_accepted_matching_source_provenance(tmp_path,change):
    discovery=rectangle_discovery(tmp_path)
    derived=discovery['rectangle_reanalysis']['edge1.tiff']
    if change=='model':derived['rectangle_model_sha256']='e'*64
    elif change=='hash':derived['source_hashes']={'edge1.tiff':'d'*64}
    else:derived['validation']['accepted']=False
    if change=='rejected':
        points,proof=full_chip.observed_edge_anchors(discovery,tmp_path,[[0,0],[100,0],[100,100],[0,100]])
        assert points is None
    else:
        with pytest.raises(SafetyError,match='provenance'):
            full_chip.observed_edge_anchors(discovery,tmp_path,[[0,0],[100,0],[100,100],[0,100]])
