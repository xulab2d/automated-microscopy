"""Model-independent checks for mask provenance, physical measurements and routing."""
from dataclasses import asdict
import json

import cv2
import numpy as np
import pytest

from flakefinder.analysis import analyze_scan
from flakefinder.colors import AnalysisOptions
from flakefinder.imaging import save_frame
from flakefinder.learned_analysis import file_sha256,rows_from_masks
from flakefinder.model import AcquisitionError,CameraTransform,Frame,Position,atomic_json


def measured_example():
    mask=np.zeros((130,180),bool);mask[35:90,50:100]=True
    # Nonconvex body and an internal hole: preserve the learned pixels and area.
    mask[35:70,75:100]=False;mask[73:80,60:65]=False
    rgb=np.full((130,180,3),2000,np.uint16)
    rgb[mask]=[1960,1900,1980]
    frame=Frame(rgb,Position(1000,2000,30),(1,1),1,metadata={'nominal_max':4095})
    return frame,mask


def test_learned_mask_preserves_nonconvex_shape_holes_area_and_weak_contrast():
    frame,mask=measured_example()
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    rows=rows_from_masks(frame,mask[None],[.35],[19],AnalysisOptions(),transform)
    assert len(rows)==1
    row=rows[0]
    assert row['disposition']=='candidate' and row['filter_reasons']==[]
    assert row['area_um2']==np.count_nonzero(mask) and row['holes_px']
    assert row['contrast_rgb']==pytest.approx([.02,.05,.01])
    assert row['learned_score']==.35 and row['layer_number'] is None
    assert row['learned_query_index']==19
    assert row['stage_xy_um']==pytest.approx(transform.stage_xy(row['centroid_px'],frame))


def test_size_requirement_does_not_become_optical_classification():
    frame,mask=measured_example();small=np.zeros_like(mask);small[8:11,8:11]=True
    # Wrong-sign color remains an inspectable proposal, not an automatic veto.
    frame.rgb[mask]=[2050,2200,2100]
    rows=rows_from_masks(frame,np.stack([mask,small]),[.65,.9],[1,2],AnalysisOptions())
    assert len(rows)==1 and rows[0]['disposition']=='candidate'
    assert rows[0]['contrast_rgb'][1]<0


def test_saved_predictions_are_verified_and_build_raw_review(tmp_path):
    frame,mask=measured_example();name='tile_000000'
    save_frame(tmp_path/'tiles',name,frame)
    source=tmp_path/'tiles'/(name+'.tiff');metadata=source.with_suffix('.json')
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    atomic_json(tmp_path/'manifest.json',{'transform':asdict(transform),'plan':{'chip_id':None,'polygon_um':None},
        'context':{'material':'MoTe2','oxide_nm':285},'completed':{name:{'position':asdict(frame.position)}}})
    predictions=tmp_path/'saved';predictions.mkdir()
    masks_path=predictions/'mask.npz'
    np.savez_compressed(masks_path,masks=mask[None],scores=np.array([.65]),query_indices=np.array([5]))
    atomic_json(predictions/'predictions.json',{'schema':1,'backend':'maskterial_hf_cpu','state':'complete',
        'fingerprint':'test-checkpoint','model':{'name':'fixture masks, no inference'},'parameters':{},'images':[
        {'source_image':str(source),'source_sha256':file_sha256(source),'metadata_sha256':file_sha256(metadata),
         'original_shape_hw':list(mask.shape),'masks_file':'mask.npz','masks_sha256':file_sha256(masks_path),
         'candidates':[{'query_index':5,'score':.65,'area_px':int(mask.sum())}],
         'focus_limited':True,'focus_status':{'status':'no_autofocus_at_this_tile'}}]})
    settings={'backend':'auto','learned':{'predictions':str(predictions/'predictions.json')}}
    summary=analyze_scan(tmp_path,settings)
    assert summary['backend']=='maskterial_hf_cpu' and summary['candidate_count']==1
    assert summary['thin_screen_count']==1 and summary['focus_limited_tiles']==1
    data=json.loads((tmp_path/'analysis/review-data.json').read_text())
    row=data['flakes'][0]
    assert row['layer_number'] is None and row['focus_limited']
    assert (tmp_path/'analysis'/row['crop_image']).is_file()
    with masks_path.open('ab') as stream:stream.write(b'tamper')
    with pytest.raises(AcquisitionError,match='mask file changed'):
        analyze_scan(tmp_path,settings)


def test_cancel_requests_checkpoint_before_stopping_worker(tmp_path,monkeypatch):
    import flakefinder.learned_analysis as module
    from flakefinder.model import Cancelled
    waits=[]
    class Worker:
        returncode=0
        def poll(self):return None
        def wait(self,timeout):
            assert (tmp_path/'STOP').exists()
            waits.append(timeout);return 0
    monkeypatch.setattr(module,'learned_runtime_available',lambda:True)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *args,**kwargs:Worker())
    def cancel():raise Cancelled('test cancellation')
    with pytest.raises(Cancelled):module._run_inference(tmp_path,tmp_path,{},None,cancel)
    assert waits==[40]
