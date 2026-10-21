import importlib.util
import json
from pathlib import Path
import sys
import numpy as np
import pytest
from flakefinder.imaging import load_frame

HERE=Path(__file__).resolve().parent
DATA=HERE.parents[1]/'data/whole-chip-standardized-01/discovery'
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value)
    return value
photo=module('_photometric_validation_test',HERE/'photometric_validation.py')
edges=module('flakefinder._photometric_edge_test',HERE/'proposed_sparse_edges.py')

def fixture(attempt,index):
    d=json.loads((DATA/attempt/'discovery.json').read_text());rec=d['records'][index]
    with np.load(DATA/attempt/rec['labels_file'])as stored:labels=stored['labels'].copy()
    return d,rec,labels


@pytest.mark.parametrize('attempt,index,accepted,refined',[
    ('candidate013',21,False,False),('candidate016',13,False,False),
    ('candidate010',9,True,False),('candidate015',6,True,True),('candidate015',7,False,False)])
def test_real_saved_raw_edge_and_counterexample(attempt,index,accepted,refined):
    d,rec,labels=fixture(attempt,index)
    proposal=edges.coherent_edge(labels,rec['position_um'],d['source_shape'],d['transform']['matrix'])
    frame=load_frame(DATA/attempt/rec['image'])
    edge,report=photo.supported_edge(frame,labels,proposal,d['transform']['matrix'],allow_raw_refinement=True)
    assert report['accepted'] is accepted
    assert (edge is not None) is accepted
    if accepted:assert report['candidate_unchanged'] is (not refined)


def test_spatial_bracket_contaminated_centre_and_unknown_seam_controls():
    for attempt,index in [('candidate014',1),('candidate013',11)]:
        _,rec,labels=fixture(attempt,index)
        assert rec['classification']=='unknown'
        result=photo.spatial_bracket_evidence(labels,'unknown')
        assert result['side']=='substrate' and result['inferred']
    assert photo.spatial_bracket_evidence(np.zeros((100,100),np.uint8),'unknown')['side']=='unknown'
    #98% substrate split by a2%-holder seam must not be inferred as one interior.
    seam=np.ones((100,100),np.uint8);seam[:,49:51]=2
    assert photo.spatial_bracket_evidence(seam,'unknown')['side']=='unknown'
    centre_dirt=np.full((100,100),2,np.uint8);centre_dirt[48:52,48:52]=0
    assert photo.spatial_bracket_evidence(centre_dirt,'unknown')['side']=='holder'


def test_registered_substrate_rejects_false_contamination_halfplane():
    d,rec,labels=fixture('candidate013',21)
    proposal=edges.coherent_edge(labels,rec['position_um'],d['source_shape'],d['transform']['matrix'])
    observations=[]
    for index in [19,20,22]:
        _,row,lab=fixture('candidate013',index)
        observations.append((row['name'],lab,row['position_um'][:2]))
    result=photo.registered_substrate_consistency(proposal,observations,d['transform']['matrix'],d['source_shape'])
    assert not result['accepted']
    assert any(r['image']=='probe_000022' and r['excluded_substrate_patches']>=3 for r in result['violations'])
