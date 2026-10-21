"""Synthetic retained-export reviews; never touch vendor hardware or real jobs."""
import json
import os
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from flakefinder import vendor_backlog as backlog, vendor_lifecycle as life
from flakefinder.model import SafetyError


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def campaign_export(tmp_path):
    project = tmp_path/'project'
    root = project/'data'/'zen-captures'
    owner = project/'data'/'scan-closed'
    identifier = 'a'*32
    folder = root/identifier
    folder.mkdir(parents=True)
    source = {'files': {'scan_worker.py':'1'*64, 'chip_campaign.py':'2'*64}}
    request = {'source_release':source, 'settings':{'light_percent':90., 'exposure_ms':100.}}
    write(owner/'scan-request.json', request)
    digest = life.file_hash(owner/'scan-request.json')
    worker = {'id':owner.name, 'pid':42, 'started_at':100.,
              'request_sha256':digest, 'source_release':source}
    write(owner/'worker-start.json',worker)
    write(owner/'scan-status.json',dict(worker, state='stopped', hardware_closed=True,
        stop_errors=[], automatic_restart=False, finished_at=123.))
    write(owner/'STOP',{'at':120.,'reason':'operator requested stop'})
    write(owner/'batch.json',{'state':'stopped','error':'STOP requested: '+str(owner/'STOP'),
        'search_bounds_um':[-100,100,-200,200],
        'reference_context':{'camera_resources':{'ff_exposure_ms':200.}}})
    # Preserve eight original companions. Exposure is the measured campaign
    # setting after adjustment, not the draft's initial exposure setting.
    for name in ('capture.czi','capture.czjob','preview.png','preview.png.metadata',
                 'job.czjob','job.czjob.metadata','capture.metadata'):
        (folder/name).write_bytes(('original '+name).encode())
    czi = folder/'capture.czi'
    props = {'File.CreationTime':'1970-01-01T00:01:58.200000Z',
        'File.Name':czi.name,'File.Size':str(czi.stat().st_size),
        'Image.StageXPosition':'10 um','Image.StageYPosition':'20 um','Image.FocusPosition':'30 um',
        'Detector.ExposureTime':'200 ms','Image.Channel.LightSourceIntensity':'90 %',
        'Instrument.ObjectiveMagnification':'20'}
    xml = ET.Element('Metadata')
    for key,value in props.items():
        ET.SubElement(xml,'Property',key=key,value=value)
    metadata = czi.with_suffix('.czi.metadata')
    ET.ElementTree(xml).write(metadata)
    for path in folder.iterdir():
        os.utime(path,(119,119))
    return root,owner,folder,metadata


def change_property(metadata, key, value):
    xml = ET.parse(metadata)
    for item in xml.getroot().findall('Property'):
        if item.attrib['key'] == key:
            item.set('value',value)
    xml.write(metadata)
    os.utime(metadata,(119,119))


def save_review(root, owner, folder):
    value = backlog.make_campaign_stop_review(root,folder.name,owner)
    path = root.parent/'vendor-retained-reviews'/(folder.name+'.json')
    write(path,value)
    return value,path


def test_closed_campaign_review_retains_all_originals_and_claims_no_receipt(campaign_export):
    root,owner,folder,_ = campaign_export
    before = {p:p.read_bytes() for p in folder.iterdir()}
    review,_ = save_review(root,owner,folder)
    assert backlog.validated_reviews(root)[folder.name] == review
    assert len(review['inventory']) == 8
    assert review['adapter_session_id'] is None
    assert review['completed_unloaded_adapter_receipt_available'] is False
    assert review['raw_acknowledged'] is False
    assert review['scientific_commit'] is False
    assert review['placement_evidence'] is False
    assert review['full_frame_duplicate_verified'] is False
    assert before == {p:p.read_bytes() for p in folder.iterdir()}
    scope = SimpleNamespace(keep_vendor_results=False,_vendor_capture_root=root,_vendor_jobs={})
    result = life.check_pending(scope)
    assert result['pending_jobs'] == 0 and result['reviewed_retained_jobs'] == 1


@pytest.mark.parametrize('change', ['source','request','raw','companion','new_file','time','closed',
    'state','stop_error','stop_time','pid','batch_stop','exposure','light','objective','x','y'])
def test_review_rejects_mutated_evidence(campaign_export,change):
    root,owner,folder,metadata = campaign_export
    save_review(root,owner,folder)
    if change == 'source':
        path=owner/'worker-start.json'; value=backlog.read(path)
        value['source_release']['files']['scan_worker.py']='f'*64; write(path,value)
    elif change == 'request':
        path=owner/'scan-request.json'; value=backlog.read(path)
        value['settings']['light_percent']=80.; write(path,value)
    elif change in ('raw','companion','new_file'):
        path=folder/({'raw':'capture.czi','companion':'preview.png','new_file':'other.metadata'}[change])
        path.write_bytes(b'changed'); os.utime(path,(119,119))
    elif change == 'time':
        change_property(metadata,'File.CreationTime','1970-01-01T00:01:50Z')
    elif change in ('closed','state','stop_error','pid'):
        path=owner/'scan-status.json'; value=backlog.read(path)
        key,new={'closed':('hardware_closed',False),'state':('state','running'),
                 'stop_error':('stop_errors',['failed to stop']),'pid':('pid',99)}[change]
        value[key]=new; write(path,value)
    elif change == 'stop_time':
        write(owner/'STOP',{'at':124.})
    elif change == 'batch_stop':
        path=owner/'batch.json'; value=backlog.read(path)
        value['error']='unrelated error'; write(path,value)
    else:
        key,new={'exposure':('Detector.ExposureTime','210 ms'),
                 'light':('Image.Channel.LightSourceIntensity','70 %'),
                 'objective':('Instrument.ObjectiveMagnification','10'),
                 'x':('Image.StageXPosition','1000 um'),
                 'y':('Image.StageYPosition','1000 um')}[change]
        change_property(metadata,key,new)
    with pytest.raises(SafetyError):
        backlog.validated_reviews(root)
    assert len(list(folder.iterdir())) >= 8


@pytest.mark.parametrize('key,value',[
    ('File.CreationTime','1970-01-01T00:01:50Z'),
    ('File.CreationTime','1970-01-01T00:02:24Z'),
    ('Detector.ExposureTime','210 ms'),
    ('Image.StageXPosition','1000 um'),
    ('Image.StageYPosition','1000 um'),
])
def test_builder_rejects_wrong_time_profile_and_bounds(campaign_export,key,value):
    root,owner,folder,metadata = campaign_export
    change_property(metadata,key,value)
    with pytest.raises(SafetyError):
        backlog.make_campaign_stop_review(root,folder.name,owner)


def test_builder_requires_closed_owner(campaign_export):
    root,owner,folder,_ = campaign_export
    path=owner/'scan-status.json'; value=backlog.read(path)
    value['hardware_closed']=False; write(path,value)
    with pytest.raises(SafetyError):
        backlog.make_campaign_stop_review(root,folder.name,owner)


@pytest.mark.parametrize('source',[None,{}, {'files':{}}, {'files':[]}])
def test_builder_requires_nonempty_pinned_source(campaign_export,source):
    root,owner,folder,_ = campaign_export
    path=owner/'scan-request.json'; value=backlog.read(path)
    value['source_release']=source; write(path,value)
    digest=life.file_hash(path)
    for name in ('worker-start.json','scan-status.json'):
        path=owner/name; value=backlog.read(path)
        value['source_release']=source; value['request_sha256']=digest; write(path,value)
    with pytest.raises(SafetyError):
        backlog.make_campaign_stop_review(root,folder.name,owner)
