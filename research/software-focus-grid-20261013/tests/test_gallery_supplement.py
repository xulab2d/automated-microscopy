import hashlib
import json
from pathlib import Path
import pytest

from flakefinder.gallery import FieldGallery
from flakefinder.batch_viewer import batch_state
from flakefinder.model import atomic_json


def fixture_campaign(root):
    chip=root/'chip1';folder=chip/'analysis-test-recovery';base=chip/'analysis-staged-features'
    fingerprint='f'*64;source_hash='a'*64
    states=['recovered','excluded','unresolved','recovered']
    complete={};failures=[];fields=[];artifacts={}
    for index,status in enumerate(states):
        tile=f'tile_{index:06d}';key='chip1/'+tile
        preview=chip/'tiles'/(tile+'.jpg');preview.parent.mkdir(parents=True,exist_ok=True);preview.write_bytes(b'raw-preview')
        complete[tile]={'hashes':{'tiles/'+tile+'.tiff':source_hash}}
        failures.append({'tile_key':key,'error':'original normalization error'})
        row={'id':'recovered-'+tile,'tile_key':key,'selected_for_review':True,'polygon_px':[[1,1],[2,1],[2,2]]}
        record={'tile_key':key,'fingerprint':fingerprint,'source_identity':'identity-'+tile,
            'state':'recovered' if status=='recovered' else 'excluded','reason':'Recorded reason',
            'provenance':{'source_files_sha256':{'.tiff':source_hash}},'tile':{'tile_id':tile},
            'rows':[row] if status=='recovered' else []}
        entry={'tile_key':key,'status':status,'version':'recovery-v1','fingerprint':fingerprint,
            'source_identity':record['source_identity'],'source_tiff_sha256':source_hash,
            'record_file':'fields/'+tile+'.json','reason':'Recorded reason','proposal_count':len(record['rows']),
            'selected_count':len(record['rows']),'rejected_count':0}
        if status=='recovered':
            mask=folder/(tile+'.png');mask.parent.mkdir(parents=True,exist_ok=True);mask.write_bytes(b'mask')
            artifacts[mask.name]=hashlib.sha256(mask.read_bytes()).hexdigest()
            record['mask_sha256']=artifacts[mask.name];entry['instance_mask']=mask.name
        atomic_json(folder/entry['record_file'],record)
        artifacts[entry['record_file']]=hashlib.sha256((folder/entry['record_file']).read_bytes()).hexdigest()
        fields.append(entry)
    atomic_json(chip/'manifest.json',{'fingerprint':fingerprint,'completed':complete})
    atomic_json(root/'batch.json',{'chip_states':{'chip1':{'state':'complete'}}})
    primary={'id':'primary-wins','tile_key':'chip1/tile_000003','selected_for_review':False}
    atomic_json(base/'fields'/'primary.json',{'rows':[primary]})
    atomic_json(base/'analysis.json',{'tiles':[{'tile_id':'tile_000003','observations_file':'fields/primary.json'}],'failed_tiles':failures})
    atomic_json(base/'analysis-progress.json',{'state':'partial_with_errors','completed_tiles':1,'total_tiles':4})
    manifest={'schema':1,'kind':'analysis_recovery_supplement','supplement_id':'test',
        'chip_id':'chip1','base_analysis':base.name,'acquisition_fingerprint':fingerprint,
        'versions':{'recovery-v1':{'fingerprint':fingerprint}},'fields':fields,'artifacts':artifacts}
    atomic_json(folder/'supplement.json',manifest)
    return folder,manifest


def test_supplement_merges_only_original_failures_and_retains_provenance(tmp_path):
    fixture_campaign(tmp_path);gallery=FieldGallery(tmp_path)
    index=gallery.index();chip=index['chips'][0]
    assert [f['analysis_status'] for f in index['fields']]==['recovered','excluded','unresolved','ready']
    assert (chip['analysed_images'],chip['recovered_images'],chip['excluded_images'],chip['unresolved_images'],chip['failed_images'],chip['pending_images'])==(2,1,1,1,0,0)
    recovered=gallery.field('chip1','tile_000000')
    assert recovered['rows'][0]['id']=='recovered-tile_000000'
    assert recovered['original_analysis_error']=='original normalization error'
    assert recovered['recovery']['version']=='recovery-v1'
    assert '/analysis-test-recovery/' in recovered['instance_mask']
    assert gallery.field('chip1','tile_000003')['rows'][0]['id']=='primary-wins'
    assert gallery.field('chip1','tile_000001')['preview']
    assert gallery.field('chip1','tile_000002')['analysis_reason']=='Recorded reason'
    progress=batch_state(tmp_path)['chips'][0]['staged_analysis']
    assert (progress['completed_tiles'],progress['recovered_tiles'],progress['excluded_tiles'],progress['unresolved_tiles'],progress['failed_tiles'])==(2,1,1,1,0)


@pytest.mark.parametrize('damage',['checksum','escape','identity','schema'])
def test_malformed_supplement_fails_closed_without_hiding_raw_fields(tmp_path,damage):
    folder,manifest=fixture_campaign(tmp_path);gallery=FieldGallery(tmp_path)
    assert gallery.field('chip1','tile_000000')['analysis_status']=='recovered'
    if damage=='checksum':
        (folder/'tile_000000.png').write_bytes(b'changed-mask')
    elif damage=='escape':
        outside=folder.parent/'outside.json';outside.write_bytes(b'{}')
        manifest['artifacts']['../outside.json']=hashlib.sha256(b'{}').hexdigest()
    elif damage=='identity':
        manifest['fields'][0]['source_tiff_sha256']='b'*64
    else:
        manifest['schema']=99
    atomic_json(folder/'supplement.json',manifest)
    index=gallery.index()
    assert index['chips'][0]['failed_images']==3
    assert index['chips'][0]['supplement_errors']
    field=gallery.field('chip1','tile_000000')
    assert field['analysis_status']=='failed' and not field['rows'] and field['preview']
    assert gallery.field('chip1','tile_000003')['rows'][0]['id']=='primary-wins'
