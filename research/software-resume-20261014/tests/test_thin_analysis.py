from dataclasses import asdict
import json
import numpy as np

from flakefinder.imaging import save_frame
from flakefinder.model import CameraTransform,Frame,Position,atomic_json
from flakefinder.thin_analysis import analyze_thin_scan


def test_complete_pipeline_detects_a_faint_plateau_and_reuses_checkpoint(tmp_path):
    rng=np.random.default_rng(17)
    rgb=rng.normal(2000,20,(192,192,3))
    rgb[60:112,64:116]-=np.array([.04,.07,.01])*2000
    frame=Frame(np.uint16(rgb),Position(100,100,30),(.25,.25),1,metadata={'white_level':4095})
    save_frame(tmp_path/'tiles','tile_000000',frame)
    transform=CameraTransform([[.25,0],[0,.25]],(.25,.25),True)
    atomic_json(tmp_path/'manifest.json',{'transform':asdict(transform),'plan':{'chip_id':'synthetic'},
        'completed':{'tile_000000':{'focus':{'success':True}}},
        'context':{'material':'MoTe2','substrate':'SiO2/Si','oxide_nm':285}})
    options={'regions':{'block_size_um':.5}}
    out=tmp_path/'result';result=analyze_thin_scan(tmp_path,options,out)
    assert not result['failed_tiles'] and result['candidate_count']==1
    row=next(r for r in json.loads((out/'flakes.json').read_text())['flakes'] if r['disposition']=='candidate')
    assert row['thin_screen']['compatible'] and row['layer_number'] is None
    assert row['area_um2']>140 and row['usable_width_um']>10
    mask=(out/'synthetic__tile_000000.instances.png').read_bytes()
    resumed=analyze_thin_scan(tmp_path,options,out)
    assert resumed['reused_fields']==1 and resumed['candidate_count']==1
    assert (out/'synthetic__tile_000000.instances.png').read_bytes()==mask
    # Updating recorded optical provenance invalidates the field checkpoint.
    metadata_path=tmp_path/'tiles/tile_000000.json'
    metadata=json.loads(metadata_path.read_text());metadata['metadata']['exposure_ms']=123
    atomic_json(metadata_path,metadata)
    changed=analyze_thin_scan(tmp_path,options,out)
    assert changed['reused_fields']==0
    # An unreadable/missing sidecar must not erase the good field or abort all
    # remaining analysis. The final result explicitly records the partial run.
    save_frame(tmp_path/'tiles','tile_000001',frame)
    (tmp_path/'tiles/tile_000001.json').unlink()
    manifest_path=tmp_path/'manifest.json'
    manifest=json.loads(manifest_path.read_text())
    manifest['completed']['tile_000001']={'focus':{'success':True}}
    atomic_json(manifest_path,manifest)
    partial=analyze_thin_scan(tmp_path,options,out)
    assert partial['candidate_count']==1 and len(partial['tiles'])==1
    assert [f['tile_key'] for f in partial['failed_tiles']]==['synthetic/tile_000001']
    assert json.loads((out/'progress.json').read_text())['state']=='complete_with_errors'
