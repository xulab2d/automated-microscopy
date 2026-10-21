"""Grid hints reorder bounded probes and never manufacture completed chips."""
from dataclasses import asdict,replace
import json

import pytest

import flakefinder.chip_campaign as campaign
from flakefinder.model import atomic_json
from test_chip_campaign_continuation import saved_campaign,add_completed_chip,install_fake


def square(x,y,size=100):
    return [[x,y],[x+size,y],[x+size,y+size],[x,y+size]]


def test_grid_options_validate_and_canonicalize_without_hardware():
    options=campaign.CampaignOptions(5000,max_chips=16,grid_prior={})
    assert options.grid_prior['rows']==4 and options.grid_prior['columns']==4
    assert options.grid_prior['spacing_um']==10000
    with pytest.raises((ValueError,TypeError)):
        campaign.CampaignOptions(5000,grid_prior={'rows':0})


def test_grid_budget_ignores_estimates_and_groups_overlapping_measured_chips():
    states={f'chip{i:03}':{'boundary_status':'estimated'} for i in range(16)}
    options=campaign.CampaignOptions(5000,max_chips=16,grid_prior={})
    assert campaign.campaign_chip_budget_count(options,states,[])==0
    # A transitive overlap must collapse all three records into one chip.
    measured=[('a',square(0,0)),('b',square(150,0)),('bridge',square(75,0)),('c',square(10000,0))]
    assert campaign.campaign_chip_budget_count(options,states,measured)==2
    assert campaign.campaign_chip_budget_count(replace(options,grid_prior=None),states,measured)==16


def test_grid_prior_exhausts_original_lattice_despite_unvalidated_chip_cap(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    add_completed_chip(saved_campaign) # Saved raster lacks a measured enclosure.
    options=replace(options,max_chips=1,grid_prior={})
    state=json.loads((out/'batch.json').read_text())
    state['options']=asdict(options)
    atomic_json(out/'batch.json',state)
    scope=install_fake(monkeypatch,frame)
    import flakefinder.grid_search as grid
    calls=[]
    def order(plan,pending,current,fit):
        calls.append(set(pending))
        return sorted(pending,reverse=True)
    monkeypatch.setattr(grid,'grid_probe_order',order)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,
        continue_discovery=True,allow_source_change=True)
    assert result['state']=='search_sampling_complete'
    assert result['remaining_lattice_indices']==[] and calls
    assert [r['index'] for r in result['seed_records']]==list(reversed(range(search['probe_count'])))
    assert result['expected_chip_count']==16 and result['measured_distinct_chip_count']==0
    assert result['expected_chip_count_remaining']==16 and result['grid_target_status']=='unresolved'
    assert result['all_chips_found'] is False
    assert all(0<=x<=100 and 0<=y<=100 for x,y in scope.travels)


def test_legacy_saved_options_without_grid_field_still_reach_source_gate(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    state=json.loads((out/'batch.json').read_text())
    state['options'].pop('grid_prior')
    atomic_json(out/'batch.json',state)
    scope=install_fake(monkeypatch,frame)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,
        continue_discovery=True,allow_source_change=True)
    assert result['state']=='search_sampling_complete' and scope.closed
