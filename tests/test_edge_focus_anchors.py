"""Edge anchor positions come from photographed edges, not polygon corners."""
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from flakefinder.chip_discovery import file_hash
from flakefinder.full_chip import observed_edge_anchors,FocusROIAdapter,edge_focus_roi
from flakefinder.model import Frame,Position,CameraTransform,AcquisitionError


def test_anchors_equal_observed_edge_means_and_keep_source_hashes(tmp_path):
    records=[]
    definitions=[([[40,80],[50,80],[60,80]],[0,1],80),
                 ([[80,40],[80,50],[80,60]],[1,0],80),
                 ([[40,20],[50,20],[60,20]],[0,-1],-20),
                 ([[20,40],[20,50],[20,60]],[-1,0],-20)]
    for index,(points,normal,offset) in enumerate(definitions):
        image=f'edge{index}.tiff';(tmp_path/image).write_bytes(b'preserved raw evidence')
        records.append({'image':image,'hashes':{image:file_hash(tmp_path/image)},
                        'edge':{'points_um':points,'normal':normal,'offset_um':offset}})
    anchors,evidence=observed_edge_anchors({'seed_xy_um':[50,50],'records':records},tmp_path,
                                         [[0,0],[100,0],[100,100],[0,100]])
    assert {tuple(p) for p in anchors}=={(50,80),(80,50),(50,20),(20,50)}
    assert len(evidence['anchors'])==4
    assert all(r['source_hashes'] and r['observed_edge_point_count']==3 for r in evidence['anchors'])


def test_edge_roi_registers_current_field_and_persists_selection(tmp_path):
    pixels=np.ones((128,128,3),np.uint16)*100;pixels[:,64:]=2000
    frame=Frame(pixels,Position(50,60,10),(1,1),0)
    calls=[]
    def capture():calls.append(1);return frame
    scope=SimpleNamespace(capture=capture)
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    adapter=FocusROIAdapter(scope,64,transform,reference_frame=frame,edge=True)
    adapter.begin_focus_anchor()
    assert calls==[1]
    roi=adapter.capture()
    assert roi.rgb.shape==(64,64,3) and roi.position==frame.position
    assert roi.metadata['focus_mask_policy']=='include_chip_edge'
    assert roi.metadata['focus_only_roi_source_px']==[32,32,96,96]
    assert np.all(roi.rgb[:,:32]==100) and np.all(roi.rgb[:,32:]==2000)
    adapter.save_focus_selection(tmp_path)
    assert (tmp_path/'full-field.tiff').exists() and (tmp_path/'selection.json').exists()
    assert roi.metadata['edge_focus_selection']['method']=='dominant_green_transition_gradient'


def test_defocused_diagonal_edge_moves_crop_off_blank_image_centre():
    yy,xx=np.mgrid[:512,:512]
    green=cv2.GaussianBlur(np.where(yy>120+.15*xx,2000.,100.).astype(np.float32),(0,0),10)
    frame=Frame(np.repeat(green[:,:,None],3,axis=2),Position(0,0,10),(1,1),0)
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    selected=edge_focus_roi(frame,64,transform)
    x,y=selected['roi_centre_px']
    assert abs(y-(120+.15*x))<3
    assert y<200 # The old central64 crop at224..288 entirely missed this edge.
    assert selected['orientation_coherence']>.9


@pytest.mark.parametrize('ramp',[False,True])
def test_uniform_field_or_unlocalized_gradient_refuses_blank_sweep(ramp):
    g=np.tile(np.linspace(200,2000,256) if ramp else np.full(256,1000.),(256,1)).astype(np.float32)
    frame=Frame(np.repeat(g[:,:,None],3,axis=2),Position(0,0,10),(1,1),0)
    with pytest.raises(AcquisitionError):edge_focus_roi(frame,64,CameraTransform([[1,0],[0,1]],(1,1),True))


def test_long_cleave_edge_survives_bright_flakes_with_conflicting_gradients():
    g=np.full((512,512),200,np.float32);g[:,:270]=2000
    for y in (40,200,360):g[y:y+60,30:150]=10000
    g=cv2.GaussianBlur(g,(0,0),2)
    frame=Frame(np.repeat(g[:,:,None],3,axis=2),Position(0,0,10),(1,1),0)
    result=edge_focus_roi(frame,64,CameraTransform([[1,0],[0,1]],(1,1),True))
    assert result['method']=='long_green_separator_line'
    assert result['orientation_coherence']<.6
    assert abs(result['edge_centre_px'][0]-269.5)<3
