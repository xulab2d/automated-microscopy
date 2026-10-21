import hashlib
import json
from pathlib import Path

from flakefinder.contrast_plot import ContrastDistribution
from flakefinder.model import atomic_json


def source(root, chip, rows, exposure=200):
    folder=root/chip;analysis=folder/'analysis-staged-features';tile='tile_000000'
    preview=folder/'tiles'/(tile+'.jpg');preview.parent.mkdir(parents=True,exist_ok=True);preview.write_bytes(b'preview')
    context={'camera':'camera','objective':'20x','exposure_ms':exposure,'white_balance':'1,1,1'}
    atomic_json(folder/'manifest.json',{'context':context,'completed':{tile:{'position':{'x':1,'y':2,'z':3}}}})
    atomic_json(analysis/'analysis.json',{'tiles':[{'tile_id':tile,'observations_file':'fields/field.json'}]})
    atomic_json(analysis/'fields/field.json',{'tile':{'shape':[100,120],'position':{'x':1,'y':2,'z':3}},'provenance':{'context':context},'rows':rows})
    return analysis/'fields/field.json'


def row(identifier, contrast=None, selected=True, area=120):
    return {'id':identifier,'selected_for_review':selected,'area_um2':area,'usable_width_um':8,
        'stage_xy_um':[11,22],'bbox_px':[10,20,30,40],'contrast_rgb':[900,900,900],
        'local_contrast_measurement':{'measurement_status':'measured' if contrast is not None else 'unmeasurable',
                                    'contrast_rgb':contrast,'reason':'saved reason'}}


def test_uses_only_measured_local_values_preserves_tails_and_separates_profiles(tmp_path):
    source(tmp_path,'chip1',[row('selected',[.1,.2,.3]),row('tail',[-27,.4,9],False,30),row('unmeasured')])
    source(tmp_path,'chip2',[row('other-profile',[.3,.4,.5])],exposure=100)
    plot=ContrastDistribution(tmp_path);data=json.loads(plot.data())
    assert data['counts']=={'total':4,'selected':3,'measured':3,'useful_measured':2,'rejected':1,'unmeasured':1}
    assert len(data['profiles'])==2
    points={p['id']:p for p in data['points']}
    assert points['tail']['c']==[-27,.4,9] and not points['tail']['u']
    assert points['selected']['c']==[.1,.2,.3] and points['selected']['xyz']==[11,22,3]
    assert 'unmeasured' not in points
    assert data['convention']=='(local substrate - feature) / local substrate'
    assert all(f['preview'].startswith('/files/') for f in data['fields'])


def test_cache_does_not_reread_unchanged_shards_and_rebuilds_only_changed_field(tmp_path):
    changed=source(tmp_path,'chip1',[row('one',[.1,.2,.3])])
    source(tmp_path,'chip2',[row('two',[.2,.3,.4])])
    plot=ContrastDistribution(tmp_path);first=json.loads(plot.data());reads=plot.shard_reads
    assert json.loads(plot.data(first['signature']))['unchanged']
    assert plot.shard_reads==reads
    saved=json.loads(changed.read_text());saved['rows'][0]['local_contrast_measurement']['contrast_rgb']=[.8,.9,1]
    atomic_json(changed,saved)
    next_data=json.loads(plot.data(first['signature']))
    assert next_data['signature']!=first['signature'] and plot.shard_reads==reads+1
    assert next(p for p in next_data['points'] if p['id']=='one')['c']==[.8,.9,1]


def test_recovery_observations_are_included_without_overriding_primary_success(tmp_path):
    from test_gallery_supplement import fixture_campaign
    folder,manifest=fixture_campaign(tmp_path)
    for index in (0,3):
        relative=f'fields/tile_{index:06d}.json';path=folder/relative
        saved=json.loads(path.read_text());saved['rows'][0].update(row('recovered-'+str(index),[.1,.2,.3]))
        atomic_json(path,saved);manifest['artifacts'][relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json(folder/'supplement.json',manifest)
    primary=tmp_path/'chip1/analysis-staged-features/fields/primary.json'
    saved=json.loads(primary.read_text());saved['rows'][0].update(row('primary-wins',[.4,.5,.6]));atomic_json(primary,saved)
    data=json.loads(ContrastDistribution(tmp_path).data())
    assert {p['id'] for p in data['points']}=={'recovered-0','primary-wins'}
    assert data['field_statuses']=={'ready':1,'recovered':1,'excluded':1,'unresolved':1,'failed':0}
    assert {f['version'] for f in data['fields']}=={'analysis-staged-features','recovery-v1'}


def test_local_plot_route_and_host_boundary(tmp_path):
    from flakefinder.batch_viewer import serve_batch
    from test_batch_viewer import request
    source(tmp_path,'chip1',[row('one',[.1,.2,.3])])
    server,_=serve_batch(tmp_path)
    try:
        assert request(server.server_port,'/contrast')[0]==200
        status,body=request(server.server_port,'/api/contrast-distribution')
        assert status==200 and len(json.loads(body)['points'])==1
        assert request(server.server_port,'/api/contrast-distribution',headers={'Host':'attacker.example'})[0]==403
    finally:
        server.shutdown();server.server_close()
