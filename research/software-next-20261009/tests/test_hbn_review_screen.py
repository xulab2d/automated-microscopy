from copy import deepcopy
import cv2
import numpy as np
import pytest
from flakefinder.hbn_review_screen import morphology_screen


def result(mask):
    return {'status':'edge_supported_proposal','original_mask':mask.copy(),'refined_mask':mask.copy(),
        'foreground_snr':20.,'metrics':{'boundary_support_fraction':.95,'median_boundary_transition':.9,
        'measured_boundary_profiles':100,'interior_relative_variation':.08,'interior_pixels':200}}


def test_sharp_uniform_polygon_passes_without_color_or_layer_claim_and_no_mutation():
    mask=np.zeros((80,80),bool);mask[20:60,15:65]=True;r=result(mask);before=deepcopy(r)
    got=morphology_screen({'class_name':'unknown'},r,(.5,.5))
    assert got['passes'] and got['metrics']['long_straight_segment_count']==4
    assert got['metrics']['nonparallel_straight_edges']
    assert 'not validated hBN identity' in got['qualification']
    assert np.array_equal(r['refined_mask'],before['refined_mask']) and r['metrics']==before['metrics']


@pytest.mark.parametrize('change,reason',[
    ({'boundary_support_fraction':.4},'weak_boundary_support'),
    ({'median_boundary_transition':.2},'diffuse_boundary_transition'),
    ({'interior_relative_variation':.4},'nonuniform_observed_interior'),
    ({'interior_relative_variation':None},'unmeasured_interior_uniformity'),
    ({'interior_pixels':8},'unmeasured_interior_uniformity')])
def test_optically_compatible_diffuse_or_unmeasured_features_stay_uncertain(change,reason):
    mask=np.zeros((80,80),bool);mask[20:60,15:65]=True;r=result(mask);r['metrics'].update(change)
    got=morphology_screen({'selected_for_review':True},r,(.5,.5))
    assert not got['passes'] and reason in got['reasons']


def test_round_blob_and_disconnected_islands_do_not_get_polygon_shortlist():
    mask=np.zeros((80,80),np.uint8);cv2.circle(mask,(40,40),13,1,-1)
    got=morphology_screen({},result(mask.astype(bool)),(.2,.2))
    assert not got['passes'] and 'insufficient_straight_edge_evidence' in got['reasons']
    large=np.zeros((180,180),np.uint8);cv2.circle(large,(90,90),65,1,-1)
    got=morphology_screen({},result(large.astype(bool)),(.5,.5))
    assert not got['passes'] and 'insufficient_straight_edge_evidence' in got['reasons']
    mask=np.zeros((80,80),bool);mask[10:30,10:30]=True;mask[50:70,50:70]=True
    got=morphology_screen({},result(mask),(.5,.5))
    assert not got['passes'] and 'disconnected_or_empty_geometry' in got['reasons']


def test_physical_scale_not_pixel_count_controls_straight_edges():
    mask=np.zeros((40,40),bool);mask[10:30,10:30]=True
    coarse=morphology_screen({},result(mask),(.5,.5))
    fine=morphology_screen({},result(np.repeat(np.repeat(mask,2,0),2,1)),(.25,.25))
    assert coarse['passes'] and fine['passes']
    assert coarse['metrics']['long_straight_segment_count']==fine['metrics']['long_straight_segment_count']==4
