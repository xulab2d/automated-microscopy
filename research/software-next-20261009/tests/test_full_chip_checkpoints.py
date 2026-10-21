"""Durable tile acceptance is independent of derived overview cadence."""
import json

import numpy as np
import pytest

import flakefinder.full_chip as full
from flakefinder.model import Cancelled,atomic_json
from test_full_chip import planned,fast_focus


def test_compact_atomic_json_has_identical_values_and_preserves_prior_on_invalid_data(tmp_path):
    data={'completed':{f'tile_{i:06d}':{'position':{'x':1.25,'y':-2.,'z':3.},
                                    'hashes':{'raw':'abc'}} for i in range(100)}}
    pretty=tmp_path/'pretty.json';compact=tmp_path/'compact.json'
    atomic_json(pretty,data);atomic_json(compact,data,compact=True)
    assert json.loads(compact.read_text())==json.loads(pretty.read_text())==data
    assert compact.stat().st_size<pretty.stat().st_size*.7
    original=compact.read_bytes()
    with pytest.raises(ValueError):atomic_json(compact,{'invalid':float('nan')},compact=True)
    assert compact.read_bytes()==original


def test_each_tile_committed_with_less_frequent_equivalent_overview(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned
    exports=[];boundaries={};classify=full.classify_boundary;coverage_calls=[]
    enclosure=full.EvidenceMap.enclosure
    def cover(evidence,*args,**kwargs):
        coverage_calls.append(1);return enclosure(evidence,*args,**kwargs)
    save_mosaic=full.Mosaic.save
    def export(mosaic,path):
        exports.append((mosaic.sum.copy(),mosaic.weight.copy()))
        return save_mosaic(mosaic,path)
    def boundary(frame,*args):
        labels=classify(frame,*args)
        boundaries[len(boundaries)]=labels.copy()
        return labels
    monkeypatch.setattr(full.Mosaic,'save',export)
    monkeypatch.setattr(full,'classify_boundary',boundary)
    monkeypatch.setattr(full.EvidenceMap,'enclosure',cover)
    callbacks=[]
    def progress(phase,frame,info):
        if phase!='tile':return
        saved=json.loads((folder/'manifest.json').read_text())
        assert len(saved['completed'])==info['index']
        row=saved['completed'][f"tile_{info['index']-1:06d}"]
        assert row['hashes'] and 'capture_and_validation' in row['acquisition_phases_s']
        callbacks.append(info['index'])
    result=full.run_full_chip(scope,plan,reference,folder,callback=progress)
    assert len(callbacks)==plan['tile_count']
    assert len(exports)==plan['tile_count']//25+1
    assert len(exports)<plan['tile_count']//10+1
    assert len(coverage_calls)==1
    expected=full.Mosaic(plan['scan_plan']['bounds_um'],max(2.,max(reference.pixel_size_um)*4,
        (plan['scan_plan']['bounds_um'][1]-plan['scan_plan']['bounds_um'][0])/3000,
        (plan['scan_plan']['bounds_um'][3]-plan['scan_plan']['bounds_um'][2])/3000))
    for index in range(plan['tile_count']):
        name=f'tile_{index:06d}'
        expected.add(full.load_frame(folder/'tiles'/(name+'.tiff')),scope.transform)
        with np.load(folder/'boundary-labels'/(name+'.npz')) as saved:
            assert np.array_equal(saved['labels'],boundaries[index])
        times=result['completed'][name]['acquisition_phases_s']
        assert times['tile_total']>=sum(v for k,v in times.items() if k!='tile_total')-1e-6
        assert all(v>=0 for v in times.values())
    assert np.array_equal(exports[-1][0],expected.sum)
    assert np.array_equal(exports[-1][1],expected.weight)


def test_stop_before_first_overview_keeps_commits_and_resumes_without_recapture(planned,fast_focus,monkeypatch):
    scope,reference,plan,folder=planned
    exports=[];original=full.Mosaic.save
    def export(mosaic,path):
        exports.append(1);return original(mosaic,path)
    monkeypatch.setattr(full.Mosaic,'save',export)
    def progress(phase,frame,info):
        if phase=='tile' and info['index']==3:(folder/'STOP').write_text('stop')
    with pytest.raises(Cancelled):
        full.run_full_chip(scope,plan,reference,folder,callback=progress)
    saved=json.loads((folder/'manifest.json').read_text())
    assert saved['state']=='stopped' and len(saved['completed'])==3
    assert not exports
    originals={p:p.read_bytes() for p in (folder/'tiles').glob('*.tiff')}
    (folder/'STOP').unlink();scope.stopped=False;scope.armed=True
    result=full.run_full_chip(scope,plan,reference,folder,resume=True)
    assert result['planned_area_acquired'] and len(result['completed'])==plan['tile_count']
    assert all(p.read_bytes()==raw for p,raw in originals.items())
    assert exports
