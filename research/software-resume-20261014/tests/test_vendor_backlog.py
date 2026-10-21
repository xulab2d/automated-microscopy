"""Synthetic retained files only; never real vendor data or a hardware client."""
from types import SimpleNamespace
import pytest
from flakefinder import vendor_backlog as backlog,vendor_lifecycle as life
from flakefinder.model import SafetyError,AcquisitionError
from test_scan_continuation import previous,preview_persistence_failure,write

@pytest.fixture
def reviewed(previous):
    project,old,_=previous
    receipt,jpg,czi,thumb,_,_=preview_persistence_failure(previous)
    root=czi.parent.parent
    for identifier in ('b'*32,'c'*32,'d'*32):
        (root/identifier).mkdir();(root/identifier/'unknown.czi').write_bytes(b'unknown; preserve')
    review=backlog.make_review(root,receipt)
    path=root.parent/'vendor-retained-reviews'/(czi.parent.name+'.json');write(path,review)
    scope=SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=root,_vendor_jobs={})
    return root,receipt,jpg,czi,thumb,path,scope

def test_only_completed_reviewed_observation_reduces_pending_and_never_deletes(reviewed):
    root,receipt,jpg,czi,thumb,path,scope=reviewed
    before={p:p.read_bytes() for p in (receipt,jpg,czi,thumb)}
    state=life.check_pending(scope)
    assert state['pending_jobs']==3 and state['reviewed_retained_jobs']==1
    identifier='e'*32;folder=root/identifier;folder.mkdir();(folder/'active.czi').write_bytes(b'active')
    scope._vendor_jobs[identifier]={'folder':str(folder)}
    with pytest.raises(AcquisitionError,match='4 jobs'):life.check_pending(scope)
    scope._vendor_jobs[identifier]['receipt']={'status':'removed'}
    assert life.check_pending(scope)['pending_jobs']==3
    assert all(p.read_bytes()==b for p,b in before.items())

@pytest.mark.parametrize('change',['jpg','czi','new_file','incomplete','unloaded','missing_observation','receipt','missing_review'])
def test_unknown_or_changed_retained_evidence_cannot_be_excluded(reviewed,change):
    root,receipt,jpg,czi,thumb,path,scope=reviewed
    life.check_pending(scope) # Changes after first capture must also be detected.
    if change=='jpg':jpg.write_bytes(b'changed')
    elif change=='czi':czi.write_bytes(b'changed')
    elif change=='new_file':(czi.parent/'new.metadata').write_bytes(b'changed')
    elif change in ('incomplete','unloaded'):
        v=backlog.read(receipt);v['capture']['job_completed' if change=='incomplete' else 'job_unloaded']=False;write(receipt,v)
    elif change=='missing_observation':receipt.unlink()
    elif change=='receipt':v=backlog.read(path);v['raw_acknowledged']=True;write(path,v)
    else:path.unlink()
    with pytest.raises((SafetyError,AcquisitionError,OSError)):life.check_pending(scope)
    assert czi.exists()


@pytest.fixture
def completed_crop(tmp_path):
    import os
    import numpy as np
    from flakefinder.imaging import save_frame
    from flakefinder.model import Frame,Position
    project=tmp_path/'project';root=project/'data'/'zen-captures';root.mkdir(parents=True)
    campaign=project/'data'/'scan-campaign';owner=project/'data'/'scan-owner'
    identifier='a'*32;folder=root/identifier;folder.mkdir();czi=folder/'capture.czi'
    czi.write_bytes(b'original full vendor image retained, not a raw duplicate')
    os.utime(czi,(5,5))
    write(campaign/'scan-request.json',{'id':campaign.name})
    write(owner/'scan-request.json',{'kind':'existing_campaign_storage_continuation_v1',
        'campaign_output':str(campaign),'parent_request_sha256':life.file_hash(campaign/'scan-request.json')})
    request_hash=life.file_hash(owner/'scan-request.json')
    write(owner/'worker-start.json',{'id':owner.name,'pid':42,'started_at':1.,
        'request_sha256':request_hash,'existing_api_invocation':True})
    write(owner/'scan-status.json',{'id':owner.name,'pid':42,'finished_at':'1970-01-01T00:00:10Z',
        'state':'stopped','hardware_closed':True,'closure':{'closed':True},'automatic_restart':False,
        'campaign_output':str(campaign),'request_sha256':request_hash})
    write(owner/'STOP',{'at':8.,'reason':'operator requested stop'})
    capture={'capture_id':identifier,'adapter_session_id':'b'*32,'result_folder':str(folder),
        'raw_rgb_sha256':'c'*64,'raw_shape':[20,20,3],'raw_dtype':'uint16','job_completed':True,'job_unloaded':True}
    frame=Frame(np.ones((10,10,3),np.uint16),Position(0,0,100),(1.,1.),3,5.,
        {'zen_vendor_result':capture,'focus_only_roi_source_px':[2,3,12,13],
         'focus_mask_policy':'include_chip_edge','physical_stage_position':{'x':0,'y':0,'z':100}})
    save_frame(campaign/'focus','best_observed',frame)
    crop=campaign/'focus'/'best_observed.tiff'
    return root,crop,owner,czi


def test_completed_crop_review_retains_original_and_only_frees_known_job(completed_crop):
    root,crop,owner,czi=completed_crop
    before=czi.read_bytes();review=backlog.make_crop_review(root,crop,owner)
    write(root.parent/'vendor-retained-reviews'/(czi.parent.name+'.json'),review)
    for identifier in ('d'*32,'e'*32,'f'*32):
        folder=root/identifier;folder.mkdir();(folder/'unknown.czi').write_bytes(b'unknown preserved')
    state=life.check_pending(SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=root,_vendor_jobs={}))
    assert state['pending_jobs']==3 and state['reviewed_retained_jobs']==1
    assert not review['raw_acknowledged'] and not review['full_frame_duplicate_verified']
    assert not review['scientific_commit'] and not review['placement_evidence']
    assert review['crop_shape']==[10,10,3] and czi.read_bytes()==before


@pytest.mark.parametrize('change',['crop','sidecar','czi','new_file','incomplete','unloaded','closure','owner','stop','after_stop','roi','missing_czi'])
def test_completed_crop_retention_rechecks_exact_evidence(completed_crop,change):
    import os
    root,crop,owner,czi=completed_crop
    review=backlog.make_crop_review(root,crop,owner)
    write(root.parent/'vendor-retained-reviews'/(czi.parent.name+'.json'),review)
    if change=='crop':crop.write_bytes(b'changed')
    elif change=='sidecar':
        data=backlog.read(crop.with_suffix('.json'));data['sequence']+=1;write(crop.with_suffix('.json'),data)
    elif change=='czi':czi.write_bytes(b'changed original')
    elif change=='new_file':(czi.parent/'unreviewed.metadata').write_text('new')
    elif change in ('incomplete','unloaded','after_stop','roi'):
        data=backlog.read(crop.with_suffix('.json'))
        if change in ('incomplete','unloaded'):data['metadata']['zen_vendor_result']['job_completed' if change=='incomplete' else 'job_unloaded']=False
        elif change=='after_stop':data['acquired_at']=9.
        else:data['metadata']['focus_only_roi_source_px']=[2,3,19,13]
        write(crop.with_suffix('.json'),data)
    elif change=='closure':
        data=backlog.read(owner/'scan-status.json');data['hardware_closed']=False;write(owner/'scan-status.json',data)
    elif change=='owner':
        data=backlog.read(owner/'scan-request.json');data['campaign_output']=str(owner);write(owner/'scan-request.json',data)
    elif change=='stop':(owner/'STOP').unlink()
    elif change=='missing_czi':czi.unlink()
    with pytest.raises((SafetyError,OSError,RuntimeError)):
        backlog.validated_reviews(root)


def test_new_unknown_capture_still_hits_unchanged_cap(completed_crop):
    root,crop,owner,czi=completed_crop
    write(root.parent/'vendor-retained-reviews'/(czi.parent.name+'.json'),backlog.make_crop_review(root,crop,owner))
    for identifier in ('d'*32,'e'*32,'f'*32,'0'*32):
        folder=root/identifier;folder.mkdir();(folder/'unknown.czi').write_bytes(b'unknown')
    with pytest.raises(AcquisitionError,match='4 jobs'):
        life.check_pending(SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=root,_vendor_jobs={}))


@pytest.fixture
def stopped_returned_capture(completed_crop):
    import os
    import xml.etree.ElementTree as ET
    root,crop,owner,czi=completed_crop;source={'files':{'acquisition_budget.py':'1'*64,'autonomous_reference.py':'2'*64,'zen.py':'3'*64}}
    request={'source_release':source,'settings':{'exposure_ms':200.,'light_percent':80.}}
    write(owner/'scan-request.json',request);sha=life.file_hash(owner/'scan-request.json')
    worker={'id':owner.name,'pid':42,'started_at':1.,'request_sha256':sha,'source_release':source}
    write(owner/'worker-start.json',worker)
    position={'x':1.,'y':2.,'z':100.};error='STOP requested: '+str(owner/'STOP')
    status={**worker,'state':'stopped','hardware_closed':True,'automatic_restart':False,'finished_at':10.,
        'stop_errors':[],'watchdog_reason':None,'failed_phase':'autonomous_reference','error_type':'Cancelled',
        'error':error,'initial_position_um':position}
    write(owner/'scan-status.json',status)
    write(owner/'autonomous-reference/proof.json',{'state':'failed','error':error,'observations':[],
        'initial_position_um':position,'xy_travel_um':0.,'budget':{'captures_started':1,'completed_actions':1}})
    props={'File.CreationTime':'1970-01-01T00:00:05Z','File.Name':czi.name,'File.Size':str(czi.stat().st_size),
        'Image.StageXPosition':'1','Image.StageYPosition':'2','Image.FocusPosition':'100 um',
        'Detector.ExposureTime':'200 ms','Image.Channel.LightSourceIntensity':'80 %','Instrument.ObjectiveMagnification':'20'}
    xml=ET.Element('Metadata')
    for key,value in props.items():ET.SubElement(xml,'Property',key=key,value=value)
    metadata=czi.with_suffix('.czi.metadata');ET.ElementTree(xml).write(metadata);os.utime(metadata,(5,5))
    return root,owner,czi,metadata


def test_stopped_returned_capture_retention_is_not_fictional_raw_ack(stopped_returned_capture):
    root,owner,czi,_=stopped_returned_capture;before=czi.read_bytes()
    review=backlog.make_stopped_capture_review(root,czi.parent.name,owner)
    write(root.parent/'vendor-retained-reviews'/(czi.parent.name+'.json'),review)
    assert backlog.validated_reviews(root)[czi.parent.name]==review
    assert review['adapter_session_id'] is None and not review['completed_unloaded_adapter_receipt_available']
    assert not review['raw_acknowledged'] and not review['scientific_commit'] and not review['full_frame_duplicate_verified']
    assert czi.read_bytes()==before


@pytest.mark.parametrize('change',['closure','request','motion','unreturned','two_captures','prior_capture','position','profile','inventory','raw'])
def test_stopped_capture_review_rejects_missing_or_changed_association(stopped_returned_capture,change):
    import os
    root,owner,czi,metadata=stopped_returned_capture
    review=backlog.make_stopped_capture_review(root,czi.parent.name,owner)
    write(root.parent/'vendor-retained-reviews'/(czi.parent.name+'.json'),review)
    if change=='closure':
        v=backlog.read(owner/'scan-status.json');v['hardware_closed']=False;write(owner/'scan-status.json',v)
    elif change=='request':write(owner/'scan-request.json',{'changed':True})
    elif change in ('motion','unreturned','two_captures'):
        path=owner/'autonomous-reference/proof.json';v=backlog.read(path)
        if change=='motion':v['xy_travel_um']=1.
        else:v['budget']['completed_actions' if change=='unreturned' else 'captures_started']=0 if change=='unreturned' else 2
        write(path,v)
    elif change in ('prior_capture','position','profile'):
        text=metadata.read_text()
        if change=='prior_capture':text=text.replace('00:00:05','00:00:00')
        elif change=='position':text=text.replace('key="Image.StageXPosition" value="1"','key="Image.StageXPosition" value="99"')
        else:text=text.replace('200 ms','500 ms')
        metadata.write_text(text);os.utime(metadata,(5,5))
    elif change=='inventory':(czi.parent/'unknown.bin').write_bytes(b'unknown')
    else:czi.write_bytes(b'changed raw')
    with pytest.raises((SafetyError,OSError)):
        backlog.validated_reviews(root)
