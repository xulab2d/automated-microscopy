"""Raw-image review, annotation provenance, and loopback write boundaries."""
from dataclasses import asdict
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest

from flakefinder.imaging import save_frame
from flakefinder.model import CameraTransform, Frame, Position, atomic_json
from flakefinder.review import build_review, serve_review, validate_reviews
from flakefinder.review import catalog_projections, pack_contrast_rg


@pytest.fixture
def raw_scan(tmp_path):
    frame=Frame(np.full((60,80,3),1000,np.uint16),Position(1000,2000,30),(.5,.5),0)
    save_frame(tmp_path/'tiles','tile_000000',frame)
    transform=CameraTransform([[.5,.05],[.05,-.5]],(.5,.5),True)
    atomic_json(tmp_path/'manifest.json',{'transform':asdict(transform),'plan':{'chip_id':None},
        'completed':{'tile_000000':{}},'context':{'objective':'20x'}})
    return tmp_path


def test_raw_tiles_do_not_require_detector_results(raw_scan):
    original=(raw_scan/'tiles/tile_000000.tiff').read_bytes()
    build_review(raw_scan)
    data=json.loads((raw_scan/'review-data.json').read_text())
    assert data['flakes']==[]
    tile=data['summary']['tiles'][0]
    assert tile['context']['objective']=='20x'
    assert tile['shape']==[60,80]
    assert (raw_scan/tile['image']).exists()
    assert (raw_scan/'tiles/tile_000000.tiff').read_bytes()==original


def annotation_body(data):
    return {'dataset_id':data['dataset_id'],'annotations':{},'rois':{
        'roi-test':{'tile_key':'tile_000000','label':'debris','polygon_px':[[10,10],[30,10],[30,20],[10,20]],'note':'Tape example'}}}


def test_manual_roi_uses_raw_pixels_and_server_derived_stage_coordinates(raw_scan):
    build_review(raw_scan);data=json.loads((raw_scan/'review-data.json').read_text())
    body=annotation_body(data);body['rois']['roi-test']['stage_polygon_um']=[[0,0]]*4
    clean=validate_reviews(body,data);roi=clean['rois']['roi-test']
    assert roi['area_um2']==50
    assert roi['stage_polygon_um'][0]==pytest.approx([984.275,2008.275])
    assert roi['source_sha256']==data['summary']['tiles'][0]['sha256']
    assert roi['layer_number'] is None
    assert roi['annotation_type']=='manual_visual_roi'
    for point in ([float('nan'),10],[-1,10],[80,10],[True,10]):
        body['rois']['roi-test']['polygon_px'][0]=point
        with pytest.raises(ValueError):validate_reviews(body,data)


def test_analysis_review_inherits_raw_roi_only_when_image_hash_matches(raw_scan):
    build_review(raw_scan);data=json.loads((raw_scan/'review-data.json').read_text())
    saved=validate_reviews(annotation_body(data),data)
    stale={**saved['rois']['roi-test'],'id':'roi-stale','source_sha256':'changed-pixels'}
    saved['rois']['roi-stale']=stale
    atomic_json(raw_scan/'reviews.json',saved)
    output=raw_scan/'analysis';output.mkdir()
    atomic_json(output/'flakes.json',{'flakes':[],'dataset_id':'new-analysis','source_folder':str(raw_scan)})
    build_review(output)
    inherited=json.loads((output/'review-data.json').read_text())
    assert list(inherited['rois'])==['roi-test']


def test_loopback_roi_save_keeps_token_origin_and_raw_file_restrictions(raw_scan):
    server,url=serve_review(raw_scan)
    try:
        data=json.loads(urlopen(url+'review-data.json').read())
        token=json.loads(urlopen(url+'api/session').read())['token']
        body=json.dumps(annotation_body(data)).encode()
        def post(headers):
            return urlopen(Request(url+'api/reviews',data=body,headers=headers))
        with pytest.raises(HTTPError) as missing:post({'Content-Type':'application/json'})
        assert missing.value.code==403
        with pytest.raises(HTTPError) as foreign:post({'X-Review-Token':token,'Origin':'https://example.com'})
        assert foreign.value.code==403
        assert json.loads(post({'X-Review-Token':token,'Origin':url.rstrip('/')}).read())['saved']
        saved=json.loads(urlopen(url+'api/reviews').read())
        assert saved['rois']['roi-test']['label']=='debris'
        assert saved['rois']['roi-test']['stage_polygon_um'][0]==pytest.approx([984.275,2008.275])
        with pytest.raises(HTTPError) as raw:urlopen(url+'tiles/tile_000000.tiff')
        assert raw.value.code==404
    finally:
        server.shutdown();server.server_close()


def test_source_observations_preserve_small_repeated_regions_and_exact_holes(raw_scan):
    """Catalog preferences and deduplication must not erase source evidence."""
    from PIL import Image
    build_review(raw_scan)
    tile=json.loads((raw_scan/'review-data.json').read_text())['summary']['tiles'][0]
    output=raw_scan/'analysis';output.mkdir()
    labels=np.zeros((60,80),np.uint16)
    labels[10:24,10:24]=1;labels[14:20,14:20]=0
    labels[35:37,35:39]=2
    Image.fromarray(labels).save(output/'instances.png')
    contrast=np.zeros((15,20,2),np.float32);contrast[2:6,2:6]=[-.67,-.61]
    np.savez_compressed(output/'contrast.npz',contrast_rg=contrast,original_shape_hw=[60,80])
    tile.update(instance_mask='instances.png',contrast_rg_file='contrast.npz')
    row={'id':'small-repeat','catalog_id':'canonical-on-another-tile','tile_key':tile['key'],
         'source_image':tile['source'],'tile_id':tile['tile_id'],'area_um2':2.,
         'polygon_px':[[35,35],[38,35],[38,36],[35,36]],'instance_label':2,
         'review_role':'bulk_positive_control','disposition':'rejected'}
    atomic_json(output/'analysis.json',{'tiles':[tile]})
    atomic_json(output/'flakes.json',{'flakes':[],'source_folder':str(raw_scan)})
    atomic_json(output/'observations.json',{'observations':[row],'physical_unique_count':1})
    build_review(output)
    data=json.loads((output/'review-data.json').read_text())
    assert data['flakes']==[] and data['observations']==[row]
    assert data['physical_unique_count']==1
    result=data['summary']['tiles'][0]
    assert result['observation_count']==1 and result['instance_label_count']==2
    edges=np.asarray(Image.open(output/result['instance_boundary_image']))[:,:,3]
    assert edges[35:37,35:39].any()  # tiny excluded catalog region is visible
    assert edges[13,15]>0 and edges[17,17]==0  # hole boundary and empty hole
    assert result['contrast_preview']['source_shape_hw']==[60,80]


def test_numerical_contrast_png_roundtrip_and_invalid_values(tmp_path):
    from PIL import Image
    values=np.array([[[-.8,-.5],[-.501,-.799],[np.nan,0.],[2.,-2.]]],np.float32)
    path=tmp_path/'map.png';description=pack_contrast_rg(values,path)
    rgb=np.asarray(Image.open(path)).astype(np.uint32)
    packed=(rgb[:,:,0]<<16)|(rgb[:,:,1]<<8)|rgb[:,:,2]
    decoded=np.stack([-2+((packed>>12).astype(float)-1)/4094*4,
                      -2+((packed&4095).astype(float)-1)/4094*4],axis=2)
    valid=np.isfinite(values).all(axis=2)
    assert np.max(abs(decoded[valid]-values[valid]))<.0005
    assert packed[0,2]==0 and description['packing']=='r12g12_rgb8'


def test_legacy_projection_is_clipped_and_labeled_not_a_new_detection():
    row={'id':'canonical','chip_id':'C','source_image':'a.tiff','polygon_px':[[10,10],[20,10],[20,20],[10,20]],
         'stage_polygon_um':[[10,10],[20,10],[20,20],[10,20]],'area_um2':100}
    tiles=[{'key':'a','tile_id':'a','chip_id':'C','shape':[40,40],'source':'a.tiff'},
           {'key':'b','tile_id':'b','chip_id':'C','shape':[40,40],'source':'b.tiff',
            'footprint_um':[[15,0],[54,0],[54,39],[15,39]]}]
    result=catalog_projections([row],tiles)
    assert len(result)==2
    assert result[0]['observation_kind']=='catalog_source'
    assert result[1]['observation_kind']=='projected_catalog_mask'
    assert min(p[0] for p in result[1]['polygon_px'])==0
    assert max(p[0] for p in result[1]['polygon_px'])==5
