"""Known-chip failures pause acquisition instead of wandering to new seeds."""
from dataclasses import replace
import json

import pytest

from flakefinder import chip_campaign as campaign
from flakefinder.model import Cancelled, atomic_json
from test_chip_campaign_continuation import saved_campaign, install_fake
from test_campaign_grid_prior import observed_rectangle


@pytest.mark.parametrize('grid',[False,True])
def test_observed_expected_chip_focus_failure_keeps_progress_and_never_rearms_or_searches(saved_campaign,monkeypatch,grid):
    cfg,reference,old,options,frame,transform,search=saved_campaign
    out=old.parent/'known-chip';options=replace(options,max_chips=100,expected_chip_count=1,
        scattered_search=not grid,grid_prior={} if grid else None)
    scope=install_fake(monkeypatch,frame);arms=[];scope.cancelled=False
    def arm(limits):
        arms.append(1);scope.limits=limits;scope.cancelled=False;scope.armed=True
    def check():
        if scope.cancelled:raise Cancelled('Focus failure STOP latch')
    scope.arm=arm;scope.check_cancel=check
    scope.travel=lambda *a:pytest.fail('Searched after the expected known chip failed focus')
    scope.capture=lambda:pytest.fail('Captured a new seed after known-chip failure')
    monkeypatch.setattr(campaign,'_seed_classification',lambda *a,**k:{'classification':'substrate_seed'})
    def discover(scope,xy,transform,reference,folder,*args,**kwargs):
        result={'state':'estimated','records':[], 'estimated_outline':{
            **observed_rectangle(),'uncertainty_um':5.,'orientation':{'orientation_deg':0.}}}
        atomic_json(folder/'discovery.json',result);return result
    monkeypatch.setattr(campaign,'discover_sparse_outline',discover)
    monkeypatch.setattr(campaign,'build_full_chip_plan',lambda *a,**k:{'tile_count':2,'focus_anchor_mode':'chip_edges',
        'scan_plan':{'polygon_um':[[1,1],[99,1],[99,99],[1,99]]}})
    monkeypatch.setattr(campaign,'focus_geometry_diagnostics',lambda plan:{'geometry_rejected_tiles':0})
    monkeypatch.setattr(campaign,'estimate_campaign_storage',lambda *a,**k:{'sufficient':False,'can_start':True})
    monkeypatch.setattr('flakefinder.edge_focus_reuse.discovery_edge_focus_entries',lambda folder:[])
    retained={}
    def fail(scope,plan,reference,folder,**kwargs):
        assert kwargs['storage_reserve_bytes']==10*1024**3
        assert kwargs['storage_guard_paths']==[cfg['zen']['job_output_dir']]
        atomic_json(folder/'manifest.json',{'state':'failed','completed':{'tile_000000':{'receipt':'retained'}},'tile_count':2})
        retained['manifest']=(folder/'manifest.json').read_bytes()
        scope.cancelled=True
        raise campaign.FocusSupportError('No bounded local focus support for tile_000001')
    monkeypatch.setattr(campaign,'run_full_chip',fail)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,rectangle_mode=True)
    assert result['state']=='needs_attention' and result['requires_attention']
    assert result['termination_reason']=='expected_observed_count_acquisition_unresolved'
    assert result['recovery_chip_ids']==['chip001'] and result['current_chip'] is None
    assert result['chip_states']['chip001']['state']=='focus_unresolved'
    assert result['chip_states']['chip001']['completed_tiles']==1
    assert result['completed_distinct_chip_count']==0 and result['observed_distinct_chip_count']==1
    assert result['expected_completed_chip_count_remaining']==1 and not result['all_chips_found']
    assert len(result['seed_records'])==1 and len(result['boundary_attempts'])==1
    assert result['remaining_lattice_indices'] and not result['sampling_complete']
    assert 'already observed' in result['completion_reason']
    assert arms==[1] and scope.closed and scope.cancelled
    assert (out/'chip001/manifest.json').read_bytes()==retained['manifest']
    assert (out/'plans/chip001.json').exists() and (out/'discovery/candidate001/discovery.json').exists()
    assert result['boundary_attempts'][0]['storage_preflight']=={'sufficient':False,'can_start':True}


def test_gate_counts_distinct_observed_chips_not_predictions_or_duplicate_estimates():
    opts=campaign.CampaignOptions(5000,scattered_search=True,expected_chip_count=2)
    state={'chip_states':{'a':{'observed_rectangle_outline':observed_rectangle()},
        'duplicate':{'observed_rectangle_outline':observed_rectangle(5)},'prediction':{'centre_um':[10000,10000]}}}
    state['boundary_attempts']=[{'id':'candidate001','search_outline':observed_rectangle(5)},
        {'id':'predicted','search_outline':{'status':'estimated','polygon_um':observed_rectangle(10000)['polygon_um']}}]
    assert campaign.known_chip_recovery_status(state,opts,[]) is None
    state['chip_states']['b']={'observed_rectangle_outline':observed_rectangle(10000),'planned_area_acquired':False}
    assert campaign.known_chip_recovery_status(state,opts,[])['known_chip_recovery']['observed_distinct_chip_count']==2
    for chip in state['chip_states'].values():chip['planned_area_acquired']=True
    assert campaign.known_chip_recovery_status(state,opts,[]) is None
    assert campaign.known_chip_recovery_status(state,replace(opts,expected_chip_count=None),[]) is None


def test_gate_handles_known_measured_chip_before_any_new_search():
    opts=campaign.CampaignOptions(5000,scattered_search=True,expected_chip_count=1)
    state={'chip_states':{'chip001':{'state':'stopped','planned_area_acquired':False}}}
    result=campaign.known_chip_recovery_status(state,opts,[('chip001',observed_rectangle()['polygon_um'])])
    assert result['state']=='needs_attention' and result['recovery_chip_ids']==['chip001']


def test_failed_minimum_width_remeasurement_retains_candidate_and_stops_before_reseed(saved_campaign,monkeypatch):
    cfg,reference,old,options,frame,transform,search=saved_campaign
    out=old.parent/'contradictory-width'
    options=replace(options,scattered_search=True,expected_chip_count=1)
    scope=install_fake(monkeypatch,frame)
    scope.travel=lambda *a:pytest.fail('Wandered after contradictory known-candidate geometry')
    scope.capture=lambda:pytest.fail('Captured another seed after contradictory geometry')
    monkeypatch.setattr(campaign,'_seed_classification',lambda *a,**k:{'classification':'substrate_seed'})
    retained={}
    def discover(scope,xy,transform,reference,folder,*args,**kwargs):
        result={'state':'unresolved','reason':'minimum_width_remeasurement_failed','records':[],
            'estimated_outline':{'status':'unresolved'},'seed_verification':{'accepted':True},
            'width_remeasurement':{'independent_samples_per_side':2,'verified':False}}
        atomic_json(folder/'discovery.json',result)
        retained['discovery']=(folder/'discovery.json').read_bytes()
        return result
    monkeypatch.setattr(campaign,'discover_sparse_outline',discover)
    monkeypatch.setattr(campaign,'interior_reseed',lambda *a,**k:pytest.fail('Prioritized another seed'))
    monkeypatch.setattr(campaign,'build_full_chip_plan',lambda *a,**k:pytest.fail('Scanned an unverified outline'))
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,rectangle_mode=True)
    assert result['state']=='needs_attention' and result['termination_reason']=='minimum_width_remeasurement_failed'
    assert result['recovery_attempt_id']=='candidate001' and result['requires_attention']
    assert 'contradictory chip dimensions' in result['completion_reason']
    assert result['chip_states']=={} and result['current_chip'] is None and not result['all_chips_found']
    assert len(result['seed_records'])==1 and len(result['boundary_attempts'])==1
    assert result['remaining_lattice_indices'] and not result['sampling_complete']
    assert result['boundary_attempts'][0]['geometry_recovery_required']
    path=out/'discovery/candidate001/discovery.json'
    assert path.read_bytes()==retained['discovery']
    assert result['geometry_recovery']['discovery_sha256']==campaign.file_hash(path)
    assert scope.closed


@pytest.mark.parametrize('grid',[False,True])
def test_observed_outline_plan_failure_pauses_without_chip_state_or_new_seed(saved_campaign,monkeypatch,grid):
    cfg,reference,old,options,frame,transform,search=saved_campaign
    out=old.parent/'plan-failure'
    options=replace(options,expected_chip_count=1,scattered_search=not grid,grid_prior={} if grid else None)
    scope=install_fake(monkeypatch,frame)
    scope.travel=lambda *a:pytest.fail('Searched elsewhere after known candidate planning failed')
    scope.capture=lambda:pytest.fail('Captured a new seed after known candidate planning failed')
    monkeypatch.setattr(campaign,'_seed_classification',lambda *a,**k:{'classification':'substrate_seed'})
    retained={}
    def discover(scope,xy,transform,reference,folder,*args,**kwargs):
        result={'state':'estimated','records':[],'estimated_outline':{
            **observed_rectangle(),'uncertainty_um':5.,'orientation':{'orientation_deg':0.}}}
        atomic_json(folder/'discovery.json',result)
        retained['discovery']=(folder/'discovery.json').read_bytes()
        return result
    monkeypatch.setattr(campaign,'discover_sparse_outline',discover)
    def fail_plan(*args,**kwargs):raise campaign.AcquisitionError('Measured focus support cannot cover this observed rectangle')
    monkeypatch.setattr(campaign,'build_full_chip_plan',fail_plan)
    monkeypatch.setattr(campaign,'run_full_chip',lambda *a,**k:pytest.fail('Started a raster after plan failure'))
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,rectangle_mode=True)
    assert result['state']=='needs_attention' and result['chip_states']=={}
    assert result['observed_distinct_chip_count']==1 and result['completed_distinct_chip_count']==0
    assert result['known_chip_recovery']['observed_distinct_chip_count']==1
    assert result['recovery_attempt_id']=='candidate001' and result['recovery_chip_ids']==[]
    assert result['boundary_attempts'][0]['state']=='unresolved_focus_support'
    assert result['boundary_attempts'][0]['error']=='Measured focus support cannot cover this observed rectangle'
    assert len(result['seed_records'])==1 and result['remaining_lattice_indices'] and not result['sampling_complete']
    assert not result['all_chips_found'] and 'already observed' in result['completion_reason']
    assert (out/'discovery/candidate001/discovery.json').read_bytes()==retained['discovery']
    assert scope.closed
