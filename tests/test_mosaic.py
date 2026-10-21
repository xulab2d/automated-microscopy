"""Local overview updates preserve tile placement, clipping and weighted overlap."""
import cv2
import numpy as np
import pytest

from flakefinder.model import CameraTransform,Frame,Position
from flakefinder.scan import Mosaic


def full_canvas_add(mosaic,frame,transform):
    h,w=frame.rgb.shape[:2]
    matrix=np.asarray(transform.matrix)
    offset=np.array([frame.position.x-mosaic.x0,frame.position.y-mosaic.y0])-matrix@[(w-1)/2,(h-1)/2]
    affine=np.column_stack([matrix/mosaic.scale,offset/mosaic.scale])
    feather=np.maximum(np.outer(np.hanning(h),np.hanning(w)),.02).astype(np.float32)
    size=(mosaic.weight.shape[1],mosaic.weight.shape[0])
    mosaic.sum+=cv2.warpAffine(frame.rgb.astype(np.float32)*feather[:,:,None],affine,size,flags=cv2.INTER_LINEAR)
    mosaic.weight+=cv2.warpAffine(feather,affine,size,flags=cv2.INTER_LINEAR)


def synthetic_frame(x,y,sequence,shape=(51,63)):
    yy,xx=np.indices(shape)
    rgb=np.stack([400+xx*2+sequence*30,700+yy*3,300+xx+yy],axis=-1).astype(np.uint16)
    return Frame(rgb,Position(x,y,100),(1,1),sequence)


@pytest.mark.parametrize('matrix',[
    [[1,0],[0,1]],
    [[np.cos(.47),-np.sin(.47)],[np.sin(.47),np.cos(.47)]],
    [[-1,.16],[.23,1]],
])
@pytest.mark.parametrize('scale',[.6,2.5])
def test_shifted_rotated_clipped_and_overlapping_tiles_match_reference(matrix,scale):
    transform=CameraTransform(matrix,(1,1),True)
    actual=Mosaic([-80,170,-60,140],scale)
    reference=Mosaic([-80,170,-60,140],scale)
    for i,(x,y) in enumerate([(-76.31,-55.17),(15.37,22.41),(38.74,42.68),(168.21,138.37)]):
        frame=synthetic_frame(x,y,i)
        before=frame.rgb.copy()
        actual.add(frame,transform)
        full_canvas_add(reference,frame,transform)
        assert np.array_equal(frame.rgb,before)
    # OpenCV's fixed-point bilinear rounding can select an adjacent 1/32-pixel
    # bin when the destination origin changes; tolerate that numerical effect.
    assert np.max(np.abs(actual.weight-reference.weight))<.004
    covered=(actual.weight>0)&(reference.weight>0)
    assert np.count_nonzero((actual.weight>0)!=(reference.weight>0))<=4
    assert np.max(np.abs(actual.rgb()[covered]-reference.rgb()[covered]))<2
    assert np.max(np.abs(actual.sum-reference.sum))<4


def test_integer_translation_is_exact_and_outside_tile_changes_nothing():
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    actual=Mosaic([0,200,0,180],1)
    reference=Mosaic([0,200,0,180],1)
    for i,(x,y) in enumerate([(61,52),(82,64)]):
        frame=synthetic_frame(x,y,i)
        actual.add(frame,transform)
        full_canvas_add(reference,frame,transform)
    assert np.array_equal(actual.sum,reference.sum)
    assert np.array_equal(actual.weight,reference.weight)
    actual.add(synthetic_frame(-500,-500,3),transform)
    assert np.array_equal(actual.sum,reference.sum)
    assert np.array_equal(actual.weight,reference.weight)


def test_changed_frame_shape_rebuilds_edge_weights():
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    actual=Mosaic([0,200,0,180],1)
    reference=Mosaic([0,200,0,180],1)
    for i,shape in enumerate([(51,63),(35,47)]):
        frame=synthetic_frame(100,90,i,shape)
        actual.add(frame,transform)
        full_canvas_add(reference,frame,transform)
    assert np.array_equal(actual.sum,reference.sum)
    assert np.array_equal(actual.weight,reference.weight)
