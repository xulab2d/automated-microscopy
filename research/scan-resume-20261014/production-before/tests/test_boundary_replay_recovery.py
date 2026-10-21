"""Missing derived artifacts never authorize changed raw or invented metadata."""
from pathlib import Path
import base64,hashlib,json,xml.etree.ElementTree as ET
import numpy as np
import pytest
from flakefinder import chip_campaign as campaign
from flakefinder.boundary_replay_recovery import BoundaryArtifactRecovery
from flakefinder.chip_discovery import BoundaryOptions,classify_boundary
from flakefinder.layout import SubstrateReference
from flakefinder.model import SafetyError,atomic_json
from test_chip_campaign_continuation import saved_campaign,add_completed_chip


@pytest.fixture
def recovery_case(saved_campaign):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    folder=add_completed_chip(saved_campaign);plan=json.loads((out/'plans/chip001.json').read_text())
    plan['acquisition_settings']={'camera_resources':{'ff_exposure_ms':200.},'illumination_percent':90.}
    plan['fingerprint']=hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k!='fingerprint'},sort_keys=True).encode()).hexdigest()
    atomic_json(out/'plans/chip001.json',plan)
    manifest=json.loads((folder/'manifest.json').read_text());manifest['coverage_plan_fingerprint']=plan['fingerprint']
    manifest['algorithm_source_sha256']='historical-writer-before-backup-integration'
    row=manifest['completed']['tile_000000'];row['hashes']['tiles/tile_000000.tiff']=campaign.file_hash(folder/'tiles/tile_000000.tiff')
    labels=classify_boundary(frame,SubstrateReference.from_frame(frame),np.median(frame.rgb.reshape(-1,3),axis=0),BoundaryOptions([2,98,2,98],cell_um=2))
    np.savez_compressed(folder/'boundary-labels/tile_000000.npz',labels=labels)
    row['hashes']['boundary-labels/tile_000000.npz']=campaign.file_hash(folder/'boundary-labels/tile_000000.npz')
    evidence=campaign.EvidenceMap([2,98,2,98],2);evidence.add(labels,[50,50],frame.rgb.shape,transform)
    np.savez_compressed(folder/'coverage-evidence.npz',flags=evidence.flags,cell_um=2,bounds_um=evidence.bounds)
    source=Path(campaign.__file__).parent
    files={name:campaign.file_hash(source/name) for name in ['chip_discovery.py','layout.py','imaging.py','model.py']}
    files['full_chip.py']=manifest['algorithm_source_sha256']
    atomic_json(out/'scan-request.json',{'source_release':{'files':files}})
    atomic_json(folder/'manifest.json',manifest)
    state=json.loads((out/'batch.json').read_text())
    return out,folder,state,plan,manifest,row,transform


def archive(case,*,x=50.,exposure=200.):
    out,folder,state,plan,manifest,row,transform=case
    props={'Image.StageXPosition':str(x),'Image.StageYPosition':'50','Image.FocusPosition':'20 um',
           'Detector.ExposureTime':f'{exposure} ms','Instrument.ObjectiveMagnification':'20',
           'Image.Channel.LightSourceIntensity':'90 %','Image.SizeY':'64','Image.SizeX':'64'}
    props['Image.FocusPosition']='20 \u00b5m'
    root=ET.Element('Metadata')
    for key,value in props.items():ET.SubElement(root,'Property',key=key,value=value)
    data=ET.tostring(root);name='capture.czi.metadata'
    value={'status':'removed_after_verified_raw_save','raw_tiff_sha256':row['hashes']['tiles/tile_000000.tiff'],
           'raw_sidecar_sha256':row['hashes']['tiles/tile_000000.json'],
           'capture':{'job_completed':True,'job_unloaded':True,'adapter_session_id':'owner','capture_id':'capture','raw_shape':[64,64,3]},
           'inventory':[{'name':name,'sha256':hashlib.sha256(data).hexdigest()}],
           'vendor_metadata_base64':{name:base64.b64encode(data).decode()}}
    path=folder/'tiles/tile_000000.zen-vendor.json';atomic_json(path,value)
    row['hashes']['tiles/tile_000000.zen-vendor.json']=campaign.file_hash(path)
    atomic_json(folder/'manifest.json',manifest)
    return path


def test_missing_label_replays_exact_hash_with_changed_writer_and_preserves_originals(recovery_case):
    out,folder,state,plan,manifest,row,transform=recovery_case
    path=folder/'boundary-labels/tile_000000.npz';path.unlink()
    before=(folder/'manifest.json').read_bytes()
    _,_,_,audit=campaign.restore_campaign_coverage(out,state,transform)
    proof=audit[0]['missing_artifact_replay']
    assert proof['aggregate_coverage_exact_match'] and proof['replay_recoveries'][0]['recomputed_sha256']==row['hashes']['boundary-labels/tile_000000.npz']
    assert not path.exists() and (folder/'manifest.json').read_bytes()==before


@pytest.mark.parametrize('change',['raw','label_hash','classifier_source'])
def test_recomputed_labels_require_unchanged_inputs_and_exact_output(recovery_case,change):
    out,folder,state,plan,manifest,row,transform=recovery_case
    (folder/'boundary-labels/tile_000000.npz').unlink()
    if change=='raw':(folder/'tiles/tile_000000.tiff').write_bytes(b'changed raw')
    elif change=='label_hash':row['hashes']['boundary-labels/tile_000000.npz']='0'*64
    else:
        request=json.loads((out/'scan-request.json').read_text());request['source_release']['files']['layout.py']='0'*64
        atomic_json(out/'scan-request.json',request)
    with pytest.raises(SafetyError):BoundaryArtifactRecovery(out,'chip001',state,plan,manifest).labels('tile_000000',row)


def test_vendor_registration_proof_does_not_restore_missing_metadata(recovery_case):
    out,folder,state,plan,manifest,row,transform=recovery_case;archive(recovery_case)
    path=folder/'tiles/tile_000000.json';path.unlink()
    _,_,_,audit=campaign.restore_campaign_coverage(out,state,transform)
    proof=audit[0]['missing_artifact_replay']['replay_recoveries'][0]
    assert proof['kind']=='independent_archived_vendor_registration'
    assert proof['original_sidecar_restored'] is False and proof['acquisition_timestamp_synthesized'] is False
    assert not path.exists()


def test_exact_backup_is_used_first_without_recreating_missing_metadata_or_labels(recovery_case):
    from flakefinder.tile_sidecar_backup import create_tile_sidecar_backup
    out,folder,state,plan,manifest,row,transform=recovery_case
    relative='tiles/tile_000000.flakes.json';atomic_json(folder/relative,{'flakes':[]})
    row['hashes'][relative]=campaign.file_hash(folder/relative)
    row['sidecar_backup']=create_tile_sidecar_backup(folder,'tile_000000',row['hashes'])
    atomic_json(folder/'manifest.json',manifest)
    missing=[folder/'tiles/tile_000000.json',folder/'boundary-labels/tile_000000.npz']
    for path in missing:path.unlink()
    _,_,_,audit=campaign.restore_campaign_coverage(out,state,transform)
    events=audit[0]['missing_artifact_replay']['replay_recoveries']
    assert len(events)==2 and all(e['kind']=='exact_original_sidecar_bundle_in_memory' for e in events)
    assert all(not path.exists() for path in missing)


@pytest.mark.parametrize('change',['raw','archive','xyz','optics','missing_archive'])
def test_alternate_registration_refuses_changed_or_missing_proof(recovery_case,change):
    out,folder,state,plan,manifest,row,transform=recovery_case
    path=archive(recovery_case,x=55. if change=='xyz' else 50.,exposure=300. if change=='optics' else 200.)
    (folder/'tiles/tile_000000.json').unlink()
    if change=='raw':(folder/'tiles/tile_000000.tiff').write_bytes(b'changed raw')
    elif change=='archive':path.write_text('{}')
    elif change=='missing_archive':path.unlink()
    with pytest.raises(SafetyError):BoundaryArtifactRecovery(out,'chip001',state,plan,manifest).registration('tile_000000',row)
