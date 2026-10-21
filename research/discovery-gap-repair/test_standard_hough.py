import importlib.util
import json
from pathlib import Path
import sys
import cv2
import numpy as np
import pytest
from flakefinder.sparse_edges import coherent_edge as original

ROOT=Path(__file__).resolve().parents[2]
CAMPAIGN=ROOT/'data/whole-chip-standardized-01/discovery'
path=Path(__file__).with_name('proposed_sparse_edges.py')
spec=importlib.util.spec_from_file_location('flakefinder._proposed_sparse_edges',path)
candidate=importlib.util.module_from_spec(spec);sys.modules[spec.name]=candidate;spec.loader.exec_module(candidate)


def saved(attempt,index):
    folder=CAMPAIGN/attempt
    data=json.loads((folder/'discovery.json').read_text())
    row=data['records'][index]
    with np.load(folder/row['labels_file']) as f:labels=f['labels']
    return labels,row['position_um'],data['source_shape'],data['transform']['matrix']


@pytest.mark.parametrize('index',[6,9,10])
def test_known_missed_east_edge_with_original_acceptance_gates(index):
    args=saved('candidate010',index)
    assert original(*args) is None
    result=candidate.coherent_edge(*args)
    assert result is not None
    assert result['substrate_purity']>=.78 and result['holder_purity']>=.78
    assert result['normal'][0]>.99
    assert result['local_residual_px_p95']<3
    assert len(result['points_um'])>=80


def test_existing_accepted_edges_remain_exact_and_never_invoke_fallback(monkeypatch):
    fixtures=[saved('candidate005',i) for i in (4,7,10,20)]
    fixtures += [saved('candidate010',i) for i in (12,17,25)]
    expected=[original(*args) for args in fixtures]
    assert all(expected)
    def forbidden(*args):raise AssertionError('Fallback called after a valid fast candidate')
    monkeypatch.setattr(candidate,'_standard_hough_candidate',forbidden)
    for args,old in zip(fixtures,expected):assert candidate.coherent_edge(*args)==old


def test_holder_substrate_unknown_and_dust_controls_rejected():
    controls=[np.full((267,320),value,np.uint8) for value in (0,1,2)]
    dust=np.ones((267,320),np.uint8);cv2.circle(dust,(160,133),65,2,-1);controls.append(dust)
    substrate_island=np.full((267,320),2,np.uint8);cv2.circle(substrate_island,(160,133),65,1,-1);controls.append(substrate_island)
    spots=np.ones((267,320),np.uint8)
    for x,y in [(60,60),(150,60),(240,60),(60,180),(150,180),(240,180)]:cv2.circle(spots,(x,y),22,2,-1)
    controls.append(spots)
    for labels in controls:
        assert original(labels,[0,0],labels.shape,np.eye(2)) is None
        assert candidate.coherent_edge(labels,[0,0],labels.shape,np.eye(2)) is None


def test_fallback_used_after_invalid_fast_proposal_and_limited_to_top_twelve(monkeypatch):
    labels=np.ones((267,320),np.uint8);labels[:,160:]=2
    # Short interior proposal fails side purity; fallback still runs once.
    monkeypatch.setattr(candidate.cv2,'HoughLinesP',lambda *a,**k:np.array([[[20,30,20,180]]]))
    true_hough=candidate.cv2.HoughLines
    calls=[]
    def counted(*args,**kwargs):calls.append(1);return true_hough(*args,**kwargs)
    monkeypatch.setattr(candidate.cv2,'HoughLines',counted)
    assert candidate.coherent_edge(labels,[0,0],labels.shape,np.eye(2)) is not None
    assert len(calls)==1
    # The valid thirteenth proposal is deliberately inaccessible.
    lines=np.array([[[5.,0.]]]*12+[[[160.,0.]]],np.float32)
    monkeypatch.setattr(candidate.cv2,'HoughLines',lambda *a,**k:lines)
    assert candidate.coherent_edge(labels,[0,0],labels.shape,np.eye(2)) is None
