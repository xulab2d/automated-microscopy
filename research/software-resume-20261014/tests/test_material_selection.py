from copy import deepcopy

import numpy as np
import pytest

from flakefinder.material_selection import classify_frame_regions
from flakefinder.model import AcquisitionError,Frame,Position


def setup():
    covariance=(np.eye(3)*.01**2).tolist()
    profile={'model_id':'test-reference','optical_context':{'context':{},'acquisition_settings':{}},
        'components':[{'name':'positive','role':'positive','mean':[-.65,-.6,-.6],'covariance':covariance},
                      {'name':'debris','role':'negative','mean':[.1,.05,.08],'covariance':covariance},
                      {'name':'substrate','role':'background','mean':[0,0,0],'covariance':covariance}],
        'parameters':{'interior_erosion_um':.5,'max_mahalanobis_squared':11.345,'likelihood_ratio':3.,
            'minimum_interior_occupancy':.6,'maximum_positive_occupancy_for_exclusion':.1,
            'minimum_debris_occupancy_for_exclusion':.6,'catalog_supported_area_um2':100.,'source_min_area_um2':.25}}
    frame=Frame(np.zeros((70,70,3),np.uint16),Position(0,0,0),(1,1),0)
    return frame,profile,[{'id':'object','instance_label':1}]


def test_disconnected_supported_specks_cannot_sum_into_useful_area():
    frame,profile,rows=setup();labels=np.zeros((70,70),np.uint16);contrast=np.zeros((70,70,3),float)
    for y in range(5,60,12):
        for x in range(5,60,12):labels[y:y+4,x:x+4]=1
    contrast[labels>0]=[-.65,-.6,-.6]
    result=classify_frame_regions(frame,contrast,labels,rows,profile)['object']
    assert result['status']=='material_like' and result['occupancy']==1
    assert result['supported_area_um2']>100 and result['largest_supported_area_um2']==16
    assert not result['catalog_selected']


def test_overlapping_reference_and_debris_stays_uncertain():
    frame,profile,rows=setup();labels=np.ones((70,70),np.uint16)
    profile['components'][1]['mean']=list(profile['components'][0]['mean'])
    contrast=np.tile([-.65,-.6,-.6],(70,70,1))
    result=classify_frame_regions(frame,contrast,labels,rows,profile)['object']
    assert result['status']=='uncertain' and not result['catalog_selected']


def test_unmatched_positive_red_green_is_preserved_as_faint_unknown():
    frame,profile,rows=setup();labels=np.ones((70,70),np.uint16)
    contrast=np.tile([.04,.075,-.04],(70,70,1))
    result=classify_frame_regions(frame,contrast,labels,rows,profile)['object']
    assert result['status']=='uncertain' and result['screening_role']=='faint_unknown'
    assert result['faint_direction_compatible'] and result['layer_number'] is None


def test_frame_profile_mismatch_is_not_silently_classified():
    frame,profile,rows=setup();profile['optical_context']['acquisition_settings']={'objective_position':2}
    frame.metadata['objective_position']=3
    with pytest.raises(AcquisitionError,match='objective_position'):
        classify_frame_regions(frame,np.zeros((70,70,3)),np.ones((70,70),np.uint16),rows,profile)
