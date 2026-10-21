"""Campaign launch checks must run without hardware or output mutations."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

import flakefinder.chip_campaign as campaign
from flakefinder.full_chip import FullChipOptions
from flakefinder.model import SafetyError


def config():
    return {'backend':'zen','zen':{'xy_backlash_um':30,'job_template':'Camera'},
            'limits':{'x_min':-100,'x_max':100,'y_min':-200,'y_max':200,'z_min':0,'z_max':100}}


@pytest.mark.parametrize('bounds',[[-100,100,-170,200],[-70,100,-200,200],[-70,101,-170,200]])
def test_invalid_envelope_precedes_read_write_and_connect(tmp_path,monkeypatch,bounds):
    def forbidden(*args,**kwargs):pytest.fail('Invalid envelope reached I/O or hardware')
    for name in ('connect','load_frame','save_frame','atomic_json'):
        monkeypatch.setattr(campaign,name,forbidden)
    output=tmp_path/'not-created'
    with pytest.raises(SafetyError,match='negative backlash'):
        campaign.run_chip_campaign(config(),tmp_path/'missing.tiff',output,bounds,
                                   campaign.CampaignOptions(80))
    assert not output.exists()


def test_exact_backlash_margin_allowed_and_bad_backlash_rejected():
    result=campaign.validate_campaign_envelope(config(),[-70,100,-170,200])
    assert result['negative_backlash_margin_um']==30
    for invalid in (-1,float('nan'),float('inf')):
        cfg=config();cfg['zen']['xy_backlash_um']=invalid
        with pytest.raises(SafetyError,match='backlash'):
            campaign.validate_campaign_envelope(cfg,[-70,100,-170,200])


def plan():
    return {'tile_count':100,'anchor_xy_um':[[0,0],[1,0],[0,1],[1,1]],
            'options':asdict(FullChipOptions(focus_search_bounds_um=[1,99]))}


def test_storage_combines_vendor_and_output_on_same_volume_without_writes(tmp_path,monkeypatch):
    cfg=config();cfg['zen']['job_output_dir']=str(tmp_path/'vendor')
    output=tmp_path/'uncreated-scan'
    monkeypatch.setattr(campaign,'_existing_volume',lambda path:('same',tmp_path))
    checks=[]
    def usage(path):checks.append(path);return SimpleNamespace(free=10**15)
    result=campaign.estimate_campaign_storage(cfg,plan(),30_000_000,output,
        geometry={'geometry_rejected_tiles':3},disk_usage=usage)
    assert len(checks)==1 and len(result['volumes'])==1
    volume=result['volumes'][0]
    assert {d['kind'] for d in volume['destinations']}=={'scan_and_derived','vendor_job_results'}
    assert volume['required_free_bytes']==sum(d['estimated_new_bytes'] for d in volume['destinations'])+10*1024**3
    assert result['reserved_local_focus_attempts']==8 # 3 geometric + 5% unanticipated
    assert result['full_frame_captures_per_focus_allowance']>=60
    assert result['sufficient']
    assert not output.exists() and not (tmp_path/'vendor').exists()


def test_storage_shortfall_on_vendor_volume_is_not_hidden_by_large_scan_volume(tmp_path,monkeypatch):
    cfg=config();cfg['zen']['job_output_dir']=str(tmp_path/'vendor')
    def volume(path):return ('vendor' if path.name=='vendor' else 'scan',path)
    monkeypatch.setattr(campaign,'_existing_volume',volume)
    def usage(path):return SimpleNamespace(free=1 if path.name=='vendor' else 10**15)
    result=campaign.estimate_campaign_storage(cfg,plan(),30_000_000,tmp_path/'scan',
        geometry={'geometry_rejected_tiles':0},disk_usage=usage)
    assert len(result['volumes'])==2 and not result['sufficient']
    assert next(v for v in result['volumes'] if v['volume_id']=='scan')['sufficient']
    assert not next(v for v in result['volumes'] if v['volume_id']=='vendor')['sufficient']
