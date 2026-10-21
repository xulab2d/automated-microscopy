import importlib.util
from pathlib import Path
import numpy as np
import pytest
from flakefinder.sparse_edges import fit_outline

spec=importlib.util.spec_from_file_location('proposed_gap_probe',Path(__file__).with_name('proposed_gap_probe.py'))
candidate=importlib.util.module_from_spec(spec);spec.loader.exec_module(candidate)


def rotated_fixture():
    angle=np.deg2rad(25);rotation=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    normals=np.array([[1,0],[0,1],[-1,0],[0,-1]])@rotation.T
    seed=np.array([0.,2000.]);bounds=[-9000,9000,-9000,9000]
    def edge(index,tag):
        normal=normals[index];point=normal*3000;tangent=np.array([-normal[1],normal[0]])
        return dict(normal=normal.tolist(),offset_um=3000.,image=tag,
                    points_um=(point+np.linspace(-120,120,40)[:,None]*tangent).tolist())
    initial=[]
    for i,direction in enumerate([[1,0],[0,1],[-1,0],[0,-1]]):
        denom=normals@direction;dist=np.where(denom>1e-8,(3000-normals@seed)/np.maximum(denom,1e-8),np.inf)
        initial.append(edge(int(np.argmin(dist)),'old_%d'%i))
    return initial,seed,bounds,normals,edge


def test_rotated_offcentre_duplicate_cardinals_find_missing_side_within_32():
    edges,seed,bounds,normals,edge=rotated_fixture()
    original=list(edges); before=fit_outline(edges,seed,bounds,75)
    assert before['status']=='unresolved' and len(before['lines'])==3
    count=14; phases=[];rays={}
    def acquire(xy,phase):
        nonlocal count
        assert count<32 and bounds[0]<=xy[0]<=bounds[1] and bounds[2]<=xy[1]<=bounds[3]
        count+=1;phases.append(phase)
        signed=normals@xy-3000;index=int(np.argmin(abs(signed))); e=None
        if abs(signed[index])<140:
            e=edge(index,'new_%d'%count);edges.append(e)
        return dict(edge=e,image='new_%d'%count,classification='substrate' if max(signed)<0 else 'holder')
    result=candidate._complete_missing_edge_directions(edges,seed,bounds,[425,355],1000,75,
        acquire,lambda record,d: record is not None and record['edge'] is not None and np.dot(record['edge']['normal'],d)>.15,
        lambda record:record['classification'],lambda:None,lambda name,ray:rays.update({name:dict(ray)}),lambda:32-count)
    assert result['status']=='estimated' and len(result['lines'])==4
    assert count<=22 and edges[:4]==original
    assert list(rays)==['gap_000'] and rays['gap_000']['status']=='coherent_edge'
    assert all('gap_' in p for p in phases)


def test_exhausted_budget_and_cancel_never_acquire():
    edges,seed,bounds,_,_=rotated_fixture()
    def unexpected(*args):raise AssertionError('Must not acquire')
    result=candidate._complete_missing_edge_directions(edges,seed,bounds,[425,355],1000,75,
        unexpected,unexpected,unexpected,lambda:None,unexpected,lambda:0)
    assert result['status']=='unresolved'
    def stop():raise RuntimeError('STOP')
    with pytest.raises(RuntimeError,match='STOP'):
        candidate._complete_missing_edge_directions(edges,seed,bounds,[425,355],1000,75,
            unexpected,unexpected,unexpected,stop,unexpected,lambda:18)
