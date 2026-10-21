import http.client
import json
from pathlib import Path
import pytest
from flakefinder.batch_viewer import serve_batch, batch_state
from flakefinder.model import atomic_json


@pytest.fixture
def viewer(tmp_path):
    server, url = serve_batch(tmp_path)
    yield tmp_path, server.server_port
    server.shutdown();server.server_close()


def request(port,path,method='GET',headers=None,body=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    c.request(method,path,body=body,headers=headers or {})
    r=c.getresponse();result=(r.status,r.read());c.close();return result


def test_registered_boundary_activity_has_live_images_and_its_own_stop(viewer):
    root,port=viewer;edge=root.parent/(root.name+'-edges');edge.mkdir()
    (edge/'frames').mkdir();(edge/'frames'/'probe_000001.jpg').write_bytes(b'boundary-preview')
    atomic_json(edge/'discovery.json',{'state':'running','probe_budget':400,'records':[
        {'phase':'boundary_frontier','image':'frames/probe_000001.tiff','position_um':[1,2,3]}]})
    atomic_json(root/'activity.json',{'kind':'boundary_discovery','folder':'../'+edge.name,'chip_id':'chip1'})
    state=json.loads(request(port,'/api/state')[1]);op=state['activity']
    assert op['state']=='running' and op['probe_count']==1
    assert request(port,op['thumbnail'])==(200,b'boundary-preview')
    assert request(port,'/activity-files/../activity.json')[0]==404
    token=json.loads(request(port,'/api/session')[1])['token']
    headers={'Origin':f'http://127.0.0.1:{port}','X-Batch-Token':token,'Content-Type':'application/json'}
    assert request(port,'/api/stop','POST',headers,'{}')[0]==200
    assert (edge/'STOP').exists() and not (root/'STOP').exists()


def test_activity_registration_cannot_escape_data_directory(viewer):
    root,port=viewer
    atomic_json(root/'activity.json',{'kind':'boundary_discovery','folder':'../../outside'})
    assert batch_state(root)['activity'] is None
    assert request(port,'/activity-files/file.jpg')[0]==404


def test_live_snapshot_and_safe_stop(viewer):
    root,port=viewer
    assert json.loads(request(port,'/api/state')[1])['state']=='waiting'
    atomic_json(root/'batch.json',{'state':'running','chip_states':{'chip1':{'state':'scanning','completed_tiles':2,'total_tiles':20}}})
    assert json.loads(request(port,'/api/state')[1])['chips'][0]['completed_tiles']==2
    token=json.loads(request(port,'/api/session')[1])['token']
    good={'Origin':f'http://127.0.0.1:{port}','X-Batch-Token':token,'Content-Type':'application/json'}
    for headers in ({}, {**good,'Origin':'https://attacker.example'}, {**good,'Host':'attacker.example'}, {**good,'X-Batch-Token':'wrong'}):
        assert request(port,'/api/stop','POST',headers,'{}')[0]==403
        assert not (root/'STOP').exists()
    assert request(port,'/api/stop','POST',good,'{}')[0]==200
    assert (root/'STOP').is_file()
    assert json.loads(request(port,'/api/state')[1])['stop_requested']


def test_raw_files_traversal_and_annotation_writes_denied(viewer):
    root,port=viewer
    (root/'raw.tiff').write_bytes(b'raw')
    (root.parent/'secret.jpg').write_bytes(b'secret')
    assert request(port,'/files/raw.tiff')[0]==404
    assert request(port,'/files/%2e%2e/secret.jpg')[0]==404
    assert request(port,'/api/state',headers={'Host':'evil.example'})[0]==403
    token=json.loads(request(port,'/api/session')[1])['token']
    headers={'Origin':f'http://127.0.0.1:{port}','X-Batch-Token':token}
    assert request(port,'/api/reviews','POST',headers,'{}')[0]==404
    assert request(port,'/api/stop','POST',headers,'{"path":"elsewhere"}')[0]==400
    assert not (root/'STOP').exists()


def test_gallery_and_thumbnail_route_are_read_only(viewer):
    root,port=viewer
    tile=root/'chip1'/'tiles';tile.mkdir(parents=True);(tile/'tile_000.jpg').write_bytes(b'image')
    analysis=root/'chip1'/'analysis';analysis.mkdir()
    html=Path('flakefinder/review.html').read_text(encoding='utf-8')
    (analysis/'review.html').write_text(html,encoding='utf-8')
    atomic_json(root/'batch.json',{'state':'running','chip_states':{'chip1':{'analysis_folder':'analysis'}}})
    chip=batch_state(root)['chips'][0]
    assert chip['thumbnail']=='/files/chip1/tiles/tile_000.jpg'
    assert chip['review_url']=='/files/chip1/analysis/review.html'
    status,body=request(port,chip['review_url']);assert status==200
    assert b'Read-only batch gallery' in body
    assert b'if(false)Promise.all' in body
    assert b'mark=()=>{}' in body
    assert request(port,chip['thumbnail'])[1]==b'image'


def test_focus_preview_requires_verified_anchor_and_totals_come_from_geometry(viewer):
    root,port=viewer
    t={'matrix':[[.2,0.],[0.,.2]],'pixel_size_um':[.2,.2],'calibrated':True}
    atomic_json(root/'configuration.json',{'camera_transform':t,'interior_scan':{'overlap':.15}})
    atomic_json(root/'batch.json',{'state':'running','patch_size_um':40,'chip_states':{
        'chip1':{'state':'focusing','total_tiles':None,'centre_um':[0,0]},
        'chip2':{'state':'queued','total_tiles':None,'centre_um':[100,100]}}})
    atomic_json(root/'chip1'/'manifest.json',{'tile_count':9,'camera_shape':[100,100,3],'transform':t})
    focus=root/'chip1'/'focus-surface';anchor=focus/'attempt'/'anchor_000';anchor.mkdir(parents=True)
    (anchor/'anchor.jpg').write_bytes(b'focus-image')
    atomic_json(focus/'focus-surface.json',{'anchors':[{'index':0,'status':'autofocus_failed','image':'attempt/anchor_000/anchor.tiff'}]})
    state=batch_state(root)
    assert state['chips'][0]['total_tiles']==9
    assert state['chips'][1]['total_tiles']==9
    assert state['chips'][0]['thumbnail'] is None
    atomic_json(focus/'focus-surface.json',{'anchors':[{'index':0,'status':'verified','image':'attempt/anchor_000/anchor.tiff'}]})
    chip=batch_state(root)['chips'][0]
    assert chip['thumbnail_kind']=='focus_anchor'
    assert chip['thumbnail_tile']=='anchor 1'
    assert request(port,chip['thumbnail'])[1]==b'focus-image'


def test_lazy_field_gallery_retains_small_rejected_features_and_pending_images(viewer):
    root,port=viewer
    chip=root/'chip1';tiles=chip/'tiles';tiles.mkdir(parents=True)
    for name in ('tile_000000','tile_000001','uncommitted'):
        (tiles/(name+'.jpg')).write_bytes(b'preview')
    atomic_json(chip/'manifest.json',{'coverage_kind':'full','camera_shape':[100,120,3],
        'completed':{'tile_000000':{'position':{'x':1,'y':2,'z':3}},'tile_000001':{}},'tile_count':2})
    atomic_json(root/'batch.json',{'coverage_kind':'full','chip_states':{'chip1':{'state':'complete'}}})
    analysis=chip/'analysis-staged-features'
    polygon=[[1,2],[3,2],[3,4],[1,4]]
    rows=[{'id':'small','area_um2':1,'polygon_px':polygon,'selected_for_review':False,'filter_reasons':['below_useful_size']},
          {'id':'large','area_um2':125,'polygon_px':polygon,'selected_for_review':True,'filter_reasons':[]}]
    atomic_json(analysis/'fields'/'tile.json',{'rows':rows,'tile':{},'report':{}})
    atomic_json(analysis/'analysis.json',{'tiles':[{'tile_id':'tile_000000','key':'chip1/tile_000000',
        'observations_file':'fields/tile.json','proposal_count':2,'selected_count':1,'rejected_count':1}]})
    status,body=request(port,'/api/gallery');assert status==200
    index=json.loads(body);assert index['image_count']==2
    assert [x['analysis_ready'] for x in index['fields']]==[True,False]
    data=json.loads(request(port,'/api/gallery-field?chip=chip1&tile=tile_000000')[1])
    assert data['counts']=={'proposal_count':2,'selected_count':1,'rejected_count':1}
    assert data['rows'][0]['polygon_px']==polygon
    assert data['rows'][0]['filter_reasons']==['below_useful_size']
    assert not data['rows'][0]['gallery_selected']
    assert data['rows'][1]['gallery_selected']
    assert request(port,data['preview'])==(200,b'preview')
    pending=json.loads(request(port,'/api/gallery-field?chip=chip1&tile=tile_000001')[1])
    assert not pending['analysis_ready'] and pending['rows']==[]
    assert request(port,'/api/gallery-field?chip=chip1&tile=uncommitted')[0]==404
    assert request(port,'/gallery')[0]==200
    # Shard replacement is observed by the next request, without a server restart.
    atomic_json(analysis/'fields'/'tile.json',{'rows':rows[:1]})
    assert json.loads(request(port,'/api/gallery-field?chip=chip1&tile=tile_000000')[1])['counts']['proposal_count']==1


def test_gallery_rejects_query_and_shard_path_escape(viewer):
    root,port=viewer
    chip=root/'chip1';tiles=chip/'tiles';tiles.mkdir(parents=True)
    (tiles/'tile_000000.jpg').write_bytes(b'preview')
    atomic_json(chip/'manifest.json',{})
    analysis=chip/'analysis-staged-features'
    atomic_json(analysis/'analysis.json',{'tiles':[{'tile_id':'tile_000000','observations_file':'../../outside.json'}]})
    atomic_json(root/'outside.json',{'rows':[{'id':'secret'}]})
    data=json.loads(request(port,'/api/gallery-field?chip=chip1&tile=tile_000000')[1])
    assert data['rows']==[] and not data['analysis_ready']
    for query in ('chip=..&tile=tile_000000','chip=chip1&tile=..%2Foutside','chip=chip1',''):
        assert request(port,'/api/gallery-field?'+query)[0]==404
    assert request(port,'/api/gallery',headers={'Host':'attacker.example'})[0]==403


def test_campaign_diagnostics_use_latest_saved_discovery(viewer):
    root,port=viewer
    atomic_json(root/'batch.json',{'coverage_kind':'full','chip_states':{}})
    atomic_json(root/'discovery'/'first'/'discovery.json',{'state':'estimated','records':[]})
    atomic_json(root/'activity.json',{'kind':'boundary_discovery','folder':'discovery/first','chip_id':'first'})
    atomic_json(root/'discovery'/'later'/'discovery.json',{'state':'stopped','records':[{'phase':'edge'}]})
    state=json.loads(request(port,'/api/state')[1])
    assert state['activity']['chip_id']=='later'
    assert state['activity']['state']=='stopped'


def test_failed_analysis_is_distinct_from_pending_and_successful_retry(viewer):
    root,port=viewer
    chip=root/'chip1';tiles=chip/'tiles';tiles.mkdir(parents=True)
    for name in ('tile_000000','tile_000001'):
        (tiles/(name+'.jpg')).write_bytes(b'raw-preview')
    atomic_json(chip/'manifest.json',{})
    atomic_json(root/'batch.json',{'chip_states':{'chip1':{'state':'complete'}}})
    analysis=chip/'analysis-staged-features'
    failed=[{'tile_key':'chip1/tile_000000','error':'Invalid fitted background.'}]
    atomic_json(analysis/'analysis.json',{'tiles':[],'failed_tiles':failed})
    atomic_json(analysis/'analysis-progress.json',{'state':'analysing','completed_tiles':0,'total_tiles':2})
    index=json.loads(request(port,'/api/gallery')[1])
    assert [x['analysis_status'] for x in index['fields']]==['failed','pending']
    assert index['chips'][0]['failed_images']==1
    assert index['chips'][0]['pending_images']==1
    field=json.loads(request(port,'/api/gallery-field?chip=chip1&tile=tile_000000')[1])
    assert field['analysis_error']=='Invalid fitted background.'
    assert field['analysis_status']=='failed' and not field['analysis_ready']
    assert request(port,field['preview'])==(200,b'raw-preview')
    progress=json.loads(request(port,'/api/state')[1])['chips'][0]['staged_analysis']
    assert progress['failed_tiles']==1 and progress['pending_tiles']==1
    # Successful retries supersede a historical failure without deleting it.
    atomic_json(analysis/'fields'/'tile.json',{'rows':[]})
    atomic_json(analysis/'analysis.json',{'tiles':[{'tile_id':'tile_000000','observations_file':'fields/tile.json'}], 'failed_tiles':failed})
    field=json.loads(request(port,'/api/gallery-field?chip=chip1&tile=tile_000000')[1])
    assert field['analysis_status']=='ready' and field['analysis_error'] is None
    assert json.loads(request(port,'/api/gallery')[1])['chips'][0]['failed_images']==0
