"""Offline protocol checks with an in-memory fake Zen, never vendor objects."""
from pathlib import Path
from types import SimpleNamespace
import json
import time
import uuid

import pytest

HERE=Path(__file__).resolve().parent


def prepare(tmp_path,enabled=True,wrong_request=False,stop=False):
    session=uuid.uuid4().hex;request=uuid.uuid4().hex
    folder=tmp_path/session;folder.mkdir()
    def write(path,value):path.write_text(json.dumps(value))
    write(tmp_path/'active.json',{'session_id':session,'deadline_unix_s':time.time()+90,
        'mode':'benchmark','count':1,'comparison_tiff':False})
    profile={'enabled':enabled,'camera_name':'Fake camera','exposure_ms':200,'readback_gates':{
        role:{'parameter_name':name,'expected':value,'kind':kind}
        for role,name,value,kind in [('white_balance','WB','0.25,1,2.6','rgb'),
          ('auto_exposure','AE',False,'boolean'),('auto_white_balance','AWB',False,'boolean'),('hdr','HDR',False,'boolean')]}}
    write(folder/'profile.json',profile)
    write(folder/'request-0001.json',{'session_id':'wrong' if wrong_request else session,
        'request_id':request,'sequence':1,'deadline_unix_s':time.time()+30})
    write(folder/'ack-0001.json',{'session_id':session,'request_id':request})
    if stop:(folder/'STOP').write_text('stop')
    calls={'snaps':0,'closes':0,'saves':[]}
    values={'WB':'0.25,1,2.6','AE':'false','AWB':'false','HDR':'false'}
    camera=SimpleNamespace(Name='Fake camera',ExposureTime=200.,Binning='1',Frame='Full',
        GetAllParameterNames=lambda:list(values),GetParameterValue=lambda name:values[name])
    def snap():
        calls['snaps']+=1
        def close():calls['closes']+=1
        return SimpleNamespace(Close=close)
    def save(image,path):
        calls['saves'].append(path);Path(path).write_bytes(b'ZISRAWFILE'+b'fake')
    zen=SimpleNamespace(Acquisition=SimpleNamespace(ActiveCamera=camera,IsLiveRunning=False,
        IsContinuousRunning=False,AcquireImage=snap),Application=SimpleNamespace(Save=save))
    source=(HERE/'macro.py').read_text(encoding='utf-8').replace('__TRANSPORT_ROOT__',repr(str(tmp_path)))
    return source,zen,folder,request,calls


def test_request_image_and_acknowledgement_have_exact_identity(tmp_path):
    source,zen,folder,identifier,calls=prepare(tmp_path)
    exec(compile(source,'isolated-macro','exec'),{'Zen':zen})
    reply=json.loads((folder/'reply-0001.json').read_text())
    assert reply['request_id']==identifier
    assert reply['czi']==identifier+'.czi'
    assert calls=={'snaps':1,'closes':1,'saves':[str(folder/(identifier+'.czi'))]}
    assert json.loads((folder/'done.json').read_text())['state']=='complete'


@pytest.mark.parametrize('options,message',[
    ({'enabled':False},'disabled'),({'wrong_request':True},'identity'),({'stop':True},'expired'),
])
def test_invalid_or_cancelled_request_never_snapshots(tmp_path,options,message):
    source,zen,folder,identifier,calls=prepare(tmp_path,**options)
    with pytest.raises(RuntimeError,match=message):exec(compile(source,'isolated-macro','exec'),{'Zen':zen})
    assert calls['snaps']==0
    assert (folder/'error.json').exists()


def test_missing_runtime_parameter_binding_blocks_snapshots(tmp_path):
    source,zen,folder,identifier,calls=prepare(tmp_path)
    profile=json.loads((folder/'profile.json').read_text())
    profile['readback_gates']['white_balance']['parameter_name']=None
    (folder/'profile.json').write_text(json.dumps(profile))
    with pytest.raises(RuntimeError,match='binding'):exec(compile(source,'isolated-macro','exec'),{'Zen':zen})
    assert calls['snaps']==0
