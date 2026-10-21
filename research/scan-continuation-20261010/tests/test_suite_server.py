import copy
import hashlib
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest

from flakefinder.suite_server import serve_suite


class Catalog:
    def __init__(self, root):
        self.root = root
        self.source = dict(dataset_id='test-suite', field_key='chip001/tile_000139', source_sha256='a'*64, shape_hw=[100,150], group_id='test-suite/chip001')
        self.row = dict(id='r1', polygons_px=[[[1,1],[40,1],[40,40],[1,40]]], area_um2=50, mask_sha256='b'*64, source_mask_sha256='c'*64)
    def catalog(self):
        return dict(dataset_id='test-suite', project_id='project-test', fields=[dict(field_key=self.source['field_key'], chip_id='chip001', tile_id='tile_000139', preview_rel='data/image.jpg', source_identity=self.source)])
    def get_field(self, key):
        return dict(source_identity=self.source, rows=[copy.deepcopy(self.row)], pixel_size_um=.2)
    def status(self):
        return dict(analysis=dict(state='storage_paused', processed_fields=41, total_fields=1266))


@pytest.fixture
def site(tmp_path):
    (tmp_path/'data').mkdir(); (tmp_path/'data/image.jpg').write_bytes(b'preview-bytes')
    provider=Catalog(tmp_path)
    server, url=serve_suite(tmp_path, 0, provider=provider)
    yield url, provider
    server.shutdown(); server.server_close()


def get(url):
    with urlopen(url) as r:
        return json.load(r)


def post(url, body, token, origin=True):
    headers={'Content-Type':'application/json','X-Review-Token':token}
    if origin: headers['Origin']=url.rstrip('/')
    req=Request(url+'api/review-events',data=json.dumps(body).encode(),headers=headers)
    with urlopen(req) as r:
        return json.load(r)


def test_reads_and_no_hardware_or_traversal(site):
    url, provider=site
    assert get(url+'api/catalog')['image_count']==1
    assert get(url+'api/status')['analysis']['state']=='storage_paused'
    assert 'no microscope' in urlopen(url).read().decode()
    assert urlopen(url+'files/data/image.jpg').read()==b'preview-bytes'
    for route in ('api/capture','files/../outside.jpg','files/data/source.tiff'):
        with pytest.raises(HTTPError) as error:
            urlopen(url+route)
        assert error.value.code in (400,404)


def test_save_bound_to_current_source_and_original_mask(site):
    url, provider=site
    token=get(url+'api/session')['token']
    field=get(url+'api/field?key=chip001/tile_000139')
    op=dict(kind='annotate', region_id='r1', labels={'semantic':'residue'}, proposal_fingerprint=field['rows'][0]['proposal_fingerprint'])
    body=dict(source_identity=field['source_identity'],expected_revision=0,actor='Human reviewer',client_event_id='test-event-01',operations=[op])
    with pytest.raises(HTTPError) as error:
        post(url,body,token,origin=False)
    assert error.value.code==403
    first=post(url,body,token)
    assert first['revision']==1
    provenance=first['regions']['r1']['source_proposal']
    assert provenance['polygons_px']==provider.row['polygons_px']
    assert post(url,body,token)['revision']==1
    provider.row['polygons_px'][0][1]=[70,1]
    changed=get(url+'api/field?key=chip001/tile_000139')
    assert changed['review']['regions']['r1']['stale_source_proposal'] is True
    with pytest.raises(HTTPError) as error:
        post(url,{**body,'expected_revision':1,'client_event_id':'test-event-02'},token)
    assert error.value.code==409
    assert get(url+'api/export')['regions'][0]['source_proposal']==provenance


def candidate_files(provider):
    folder=provider.root/'data/whole-chip-standardized-01/analysis-residue-sieves-campaign-01'
    folder.mkdir(parents=True)
    base=dict(field=provider.source['field_key'], preview='chip001/tiles/tile_000139.jpg',
              source_shape_hw=[100,150], bbox_px=[1,1,39,39], polygons_px=provider.row['polygons_px'],
              area_um2=50, source_field_reference='fields/chip001__tile_000139.json')
    return folder,base


def test_resume_candidate_union_and_preserved_committed_counts(site):
    url,provider=site
    folder,base=candidate_files(provider)
    (folder/'ledger.json').write_text(json.dumps({
        'chip001/tile_000139':{'summary':{'counts':{'uncertain':1,'rejected':8}}},
        'chip001/tile_000140':{'summary':{'counts':{'shortlist':1,'rejected':3}}}}))
    (folder/'catalog.json').write_text(json.dumps(dict(state='running',processed_fields=1,total_fields=20,
        rows=[dict(base,id='r1',disposition='uncertain',rank=.9)])))
    (folder/'resume-retained-catalog.json').write_text(json.dumps(dict(rows=[
        dict(base,id='r1',disposition='uncertain',rank=.2),dict(base,id='r2',disposition='shortlist',rank=.7)])))
    provider.status=lambda:dict(analysis=dict(state='running',processed_fields=1,total_fields=20,counts={'uncertain':1}))
    state=get(url+'api/status')['analysis']
    assert state['processed_fields']==2 and state['raw_processed_fields']==1
    assert state['phase']=='verifying_saved_fields' and state['saved_fields_verified']==1
    assert state['counts']=={'uncertain':1,'shortlist':1,'rejected':11}
    candidates=get(url+'api/candidates')
    assert candidates['retained_committed_catalog'] is True
    assert candidates['source_count']==2 and candidates['potential_count']==2
    assert [r['id'] for r in candidates['rows']]==['r2','r1']
    assert candidates['rows'][1]['rank']==.9
    assert candidates['rows'][0]['source_record_rel'].endswith('fields/chip001__tile_000139.json')
    provider.status=lambda:dict(analysis=dict(state='failed_closed',processed_fields=1,total_fields=20))
    failed=get(url+'api/candidates')
    assert failed['source_count']==2 and failed['state']=='failed_closed'
    assert failed['phase']!='verifying_saved_fields'
    (folder/'catalog.json').write_text('{')  # mid-publication / invalid snapshot
    (folder/'ledger.json').unlink()         # atomic-replace gap
    retained=get(url+'api/candidates')
    assert retained['source_count']==2 and retained['processed_fields']==2
    assert any(w['retained_snapshot'] for w in retained['live_read_warnings'])


def test_candidate_human_tape_exclusion_requires_exact_hashes(site):
    url,provider=site
    folder,base=candidate_files(provider)
    (folder/'catalog.json').write_text(json.dumps(dict(rows=[dict(base,id='r1',disposition='uncertain',rank=.5)])))
    prior=folder.parent/'analysis-residue-sieves-02/human-review.json';prior.parent.mkdir()
    review=dict(id='r1',field=provider.source['field_key'],label='tape_residue',quote='the uncertain one is tape',
                source_tiff_sha256=provider.source['source_sha256'],mask_sha256=provider.row['mask_sha256'],
                source_mask_sha256=provider.row['source_mask_sha256'])
    prior.write_text(json.dumps(dict(reviews=[review])))
    candidates=get(url+'api/candidates')
    assert candidates['source_count']==1 and candidates['potential_count']==0 and candidates['human_excluded_count']==1
    assert candidates['rows'][0]['disposition']=='uncertain'  # original scientific disposition survives
    assert candidates['rows'][0]['human_review']['semantic']=='residue'
    prior.write_text(json.dumps(dict(reviews=[{**review,'mask_sha256':'d'*64}])))
    assert get(url+'api/candidates')['potential_count']==1


def test_candidate_paths_remain_confined(site):
    url,provider=site
    folder,base=candidate_files(provider)
    (folder/'catalog.json').write_text(json.dumps(dict(rows=[dict(base,id='r1',disposition='uncertain',
        source_field_reference='../../../../outside.json')])))
    result=get(url+'api/candidates')
    assert result['rows']==[] and result['warnings']


def all_candidate_files(provider):
    folder, base = candidate_files(provider)
    shard = folder/'field-attempts/chip001__tile_000139-abcd/fields/chip001__tile_000139.json'
    shard.parent.mkdir(parents=True)
    rows = []
    for i, (disposition, sieve, state) in enumerate((
        ('rejected', 'TEXTURE', 'fail'), ('rejected', 'COLOR', 'fail'),
        ('rejected', 'EDGE', 'fail'), ('bulk_or_clipped', 'COLOR', 'unavailable'),
        ('shortlist', 'COLOR', 'pass'), ('uncertain', 'COLOR', 'unavailable'))):
        rows.append(dict(base, id=f'region-{i}', disposition=disposition, rank=i,
            contrast_rgb=[round(i*.1-.2, 2), round(i*.1-.1, 2), round(i*.1, 2)],
            source_tiff_sha256=provider.source['source_sha256'],
            mask_sha256=provider.row['mask_sha256'], source_mask_sha256=provider.row['source_mask_sha256'],
            sieves=[dict(name=sieve, status=state, reason='Saved explanation')]))
    shard.write_text(json.dumps({'rows': rows}))
    ledger={provider.source['field_key']: dict(folder=str(shard.parent.parent),
        summary={'counts':{'rejected':3,'bulk_or_clipped':1,'shortlist':1,'uncertain':1}},
        hashes={str(shard):hashlib.sha256(shard.read_bytes()).hexdigest()})}
    (folder/'ledger.json').write_text(json.dumps(ledger))
    (folder/'catalog.json').write_text(json.dumps({'rows':rows[4:]}))
    return folder, shard, ledger


def test_rejected_gallery_paginates_and_filters_independent_reasons(site):
    url, provider = site
    folder, shard, ledger = all_candidate_files(provider)
    first=get(url+'api/candidates?filter=rejected&limit=2')
    assert first['filtered_count']==3 and first['offset']==0
    assert [r['id'] for r in first['rows']]==['region-2','region-1']
    second=get(url+'api/candidates?filter=rejected&limit=2&offset=2')
    assert [r['id'] for r in second['rows']]==['region-0']
    assert first['filter_counts']==dict(potential=2,shortlist=1,uncertain=1,rejected=3,bulk_or_clipped=1,all=6,excluded=0)
    colored=get(url+'api/candidates?filter=rejected&reason=COLOR')
    assert [r['id'] for r in colored['rows']]==['region-1']
    assert colored['rows'][0]['sieves'][0]['reason']=='Saved explanation'
    bulk=get(url+'api/candidates?filter=bulk_or_clipped')
    assert bulk['rows'][0]['disposition']=='bulk_or_clipped'
    all_rows=get(url+'api/candidates?filter=all')
    assert all_rows['filtered_count']==6 and all_rows['rows'][0]['disposition']=='shortlist'
    missing=get(url+'api/candidates?filter=uncertain&reason=COLOR_UNAVAILABLE')
    assert missing['filtered_count']==1
    assert get(url+'api/candidates?filter=uncertain&reason=COLOR_UNCERTAIN')['filtered_count']==0
    # Reading and paging never modifies the source or decisions.
    assert hashlib.sha256(shard.read_bytes()).hexdigest()==ledger[provider.source['field_key']]['hashes'][str(shard)]


def test_rejected_index_rejects_hash_mismatch_and_invalid_query(site):
    url, provider=site
    folder, shard, ledger=all_candidate_files(provider)
    shard.write_text('{"rows": []}')
    result=get(url+'api/candidates?filter=all')
    assert result['rows']==[] and result['warnings'][0]['index_unavailable']
    assert 'content changed' in result['warnings'][0]['error']
    for args in ('filter=bad','filter=all&limit=1000','filter=all&offset=-1',
                 'filter=all&chip=outside','filter=all&reason=bad'):
        with pytest.raises(HTTPError) as exc:get(url+'api/candidates?'+args)
        assert exc.value.code==400


def test_human_exclusion_can_also_review_algorithm_rejected_regions(site):
    url, provider=site
    folder, shard, ledger=all_candidate_files(provider)
    provider.row['id']='region-0'
    prior=folder.parent/'analysis-residue-sieves-02/human-review.json';prior.parent.mkdir()
    prior.write_text(json.dumps({'reviews':[dict(id='region-0',field=provider.source['field_key'],
        label='tape_residue',source_tiff_sha256=provider.source['source_sha256'],
        mask_sha256=provider.row['mask_sha256'],source_mask_sha256=provider.row['source_mask_sha256'])]}))
    excluded=get(url+'api/candidates?filter=excluded')
    assert excluded['filtered_count']==1 and excluded['rows'][0]['id']=='region-0'
    rejected=get(url+'api/candidates?filter=rejected')
    assert rejected['filtered_count']==3  # saved machine decision is still inspectable
    assert next(r for r in rejected['rows'] if r['id']=='region-0')['human_excluded']


def test_contrast_brush_applies_before_pagination_with_stable_plot_context(site):
    url, provider = site
    folder, shard, ledger = all_candidate_files(provider)
    query = url+'api/candidates?filter=rejected&plot=1&limit=1'
    initial = get(query)
    assert initial['filtered_count'] == initial['contrast_plot']['plotted_count'] == 3
    assert len(initial['rows']) == 1
    assert initial['contrast_selection'] is None
    # Negative contrasts and closed boundaries; region-0 was not on page one.
    box = '&contrast_x=R&contrast_y=G&x_min=-0.2&x_max=-0.1&y_min=-0.1&y_max=0'
    selected = get(query+box)
    assert selected['filtered_count'] == 2 and selected['unbrushed_count'] == 3
    assert selected['contrast_plot'] == initial['contrast_plot']
    assert selected['rows'][0]['id'] == 'region-1'
    assert get(query+box+'&offset=1')['rows'][0]['id'] == 'region-0'
    # The rectangle intersects the existing reason/chip/category filters.
    reason = get(query+box+'&reason=COLOR&chip=chip001')
    assert reason['filtered_count'] == reason['contrast_plot']['population_count'] == 1
    assert reason['rows'][0]['id'] == 'region-1'
    blue = get(url+'api/candidates?filter=all&contrast_x=B&contrast_y=R&x_min=.3&x_max=.4&y_min=.1&y_max=.2')
    assert [r['id'] for r in blue['rows']] == ['region-4', 'region-3']
    empty = get(query+'&contrast_x=R&contrast_y=B&x_min=10&x_max=11&y_min=10&y_max=11&offset=100')
    assert empty['rows'] == [] and empty['offset'] == 0
    assert empty['contrast_plot'] == initial['contrast_plot']
    assert get(query)['filtered_count'] == 3  # Clear means the original view.
    assert hashlib.sha256(shard.read_bytes()).hexdigest() == ledger[provider.source['field_key']]['hashes'][str(shard)]


def test_contrast_missing_values_are_not_substrate_zero(site):
    from flakefinder.suite_server import rgb_contrast
    for bad in (None, [], [0, 0], [None, 0, 0], ['0', 0, 0], [False, 0, 0], [float('nan'), 0, 0], [0, float('inf'), 0]):
        assert rgb_contrast({'contrast_rgb': bad}) is None
    assert rgb_contrast({'contrast_rgb': [0, 0, 0]}) == [0, 0, 0]
    url, provider = site
    folder, shard, ledger = all_candidate_files(provider)
    data = json.loads(shard.read_text())
    data['rows'][0]['contrast_rgb'] = [None, 0, 0]
    data['rows'][1].pop('contrast_rgb')
    shard.write_text(json.dumps(data))
    ledger[provider.source['field_key']]['hashes'][str(shard)] = hashlib.sha256(shard.read_bytes()).hexdigest()
    (folder/'ledger.json').write_text(json.dumps(ledger))
    query = url+'api/candidates?filter=all&plot=1'
    result = get(query)
    assert result['filtered_count'] == 6
    assert result['contrast_plot']['missing_count'] == 2
    assert result['contrast_plot']['plotted_count'] == 4
    assert next(p for p in result['contrast_plot']['points'] if p['id'] == 'region-3')['color_status'] == 'unavailable'
    brush = get(query+'&contrast_x=G&contrast_y=B&x_min=-1&x_max=1&y_min=-1&y_max=1')
    assert brush['filtered_count'] == 4
    assert 'region-0' not in [r['id'] for r in brush['rows']]


def test_contrast_brush_rejects_incomplete_and_nonfinite_gates(site):
    url, provider = site
    all_candidate_files(provider)
    valid = 'contrast_x=R&contrast_y=G&x_min=-1&x_max=1&y_min=-1&y_max=1'
    for query in ('contrast_x=R', valid.replace('contrast_y=G','contrast_y=R'),
                  valid.replace('contrast_x=R','contrast_x=Q'), valid.replace('x_min=-1','x_min=NaN'),
                  valid.replace('x_max=1','x_max=inf'), valid.replace('x_min=-1','x_min=2'),
                  valid.replace('x_min=-1','x_min=1'), valid+'&x_min=0', 'plot=bogus'):
        with pytest.raises(HTTPError) as error:
            get(url+'api/candidates?filter=all&'+query)
        assert error.value.code == 400
