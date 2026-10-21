"""Offline finite-coverage search, count semantics and campaign integration."""
from dataclasses import asdict,replace
import json

import numpy as np
import pytest

import flakefinder.chip_campaign as campaign
from flakefinder.model import atomic_json
from flakefinder.scattered_search import next_scattered_probe,sampling_status
from test_chip_campaign_continuation import saved_campaign,install_fake
from test_campaign_grid_prior import observed_rectangle,square


def plan():
    return campaign.plan_campaign_search([-30000,30000,-20000,20000],5000,[29000,19000],scattered_search=True)


def test_reproducible_multiresolution_plan_preserves_every_certified_probe():
    p=plan();legacy=campaign.plan_campaign_search(p['bounds_um'],5000,[29000,19000])
    assert p['probe_centres_um']==legacy['probe_centres_um']
    assert sorted(p['coverage_order'])==list(range(p['probe_count']))
    assert json.loads(json.dumps(p))==plan()
    early=np.asarray(p['probe_centres_um'])[p['coverage_order'][:7]]
    assert len({(x>0,y>0) for x,y in early})==4
    points=np.asarray(p['probe_centres_um'])
    # Every test disk is fully within the finite envelope, including near sides.
    for x in np.linspace(-27500,27500,45):
        for y in np.linspace(-17500,17500,31):
            assert np.min(np.linalg.norm(points-[x,y],axis=1))<=(5000-40)/2+1e-8


def test_unknown_misses_and_wrong_estimate_cannot_erase_fallback():
    p=plan();pending=set(range(p['probe_count']));observations=[];current=p['start_xy_um'];chosen=[]
    wrong=[[-40000,-30000],[40000,-30000],[40000,30000],[-40000,30000]]
    while pending:
        result=next_scattered_probe(p,pending,current,observations=observations,estimated_outlines=[wrong])
        # JSON restart plus different set construction yields identical decision.
        assert result==next_scattered_probe(json.loads(json.dumps(p)),set(reversed(sorted(pending))),current,
            observations=json.loads(json.dumps(observations)),estimated_outlines=[wrong])
        index=result['index'];current=p['probe_centres_um'][index]
        pending.remove(index);chosen.append(index)
        observations.append({'requested_xy_um':current,'classification':'unknown'})
    assert len(set(chosen))==p['probe_count']
    status=sampling_status(p,pending,observations,measured_count=0,observed_count=0,completed_count=0,expected_count=4)
    assert status['sampling_complete'] and status['scattered_target_status']=='sampling_exhausted_with_unresolved_evidence'
    assert status['expected_chip_count_remaining']==4 and not status['all_chips_found']


def test_estimates_change_priority_but_do_not_create_chips():
    p=plan();pending=set(range(p['probe_count']));current=[0,0]
    first=next_scattered_probe(p,pending,current)
    x,y=p['probe_centres_um'][first['index']]
    outline=[[x-100,y-100],[x+100,y-100],[x+100,y+100],[x-100,y+100]]
    alternative=next_scattered_probe(p,pending,current,estimated_outlines=[outline])
    assert alternative['index']!=first['index']
    # Three collinear substrate observations are samples, never distinct chips.
    records=[{'requested_xy_um':[x,0],'classification':'substrate_seed'} for x in (0,10000,20000)]
    status=sampling_status(p,pending,records,measured_count=0,observed_count=0,completed_count=0,expected_count=3)
    assert status['observed_distinct_chip_count']==0 and status['expected_chip_count_remaining']==3
    status=sampling_status(p,pending,records,measured_count=0,observed_count=3,completed_count=2,expected_count=3)
    assert status['scattered_target_status']=='observed_count_reached_validation_or_scan_pending'
    assert status['expected_completed_chip_count_remaining']==1


def test_new_options_reject_conflicting_prior_and_keep_legacy_defaults():
    options=campaign.CampaignOptions(5000)
    old=asdict(options);old.pop('expected_chip_count');old.pop('scattered_search')
    assert campaign.campaign_options_equal(old,options)
    for value in (0,-1,True,2.5):
        with pytest.raises(ValueError):campaign.CampaignOptions(5000,expected_chip_count=value)
    with pytest.raises(ValueError):campaign.CampaignOptions(5000,scattered_search=True,grid_prior={})


def test_scattered_count_groups_duplicate_rectangles_and_ignores_point_claims():
    options=campaign.CampaignOptions(5000,scattered_search=True,expected_chip_count=2)
    states={'a':{'observed_rectangle_outline':observed_rectangle()},
            'same_a':{'observed_rectangle_outline':observed_rectangle(10)},
            'b':{'observed_rectangle_outline':observed_rectangle(10000)},
            'point':{'centre_um':[50000,50000]},'three_sides':{'observed_rectangle_outline':{
                **observed_rectangle(20000),'lines':observed_rectangle()['lines'][:3]}}}
    assert campaign.campaign_chip_budget_count(options,states,[])==2
    assert campaign.campaign_chip_budget_count(options,states,[('same_a_measured',square(0,0))])==2


def test_target_completed_is_not_sampling_complete():
    p=plan()
    status=sampling_status(p,set(range(p['probe_count'])),[],measured_count=0,observed_count=2,completed_count=2,expected_count=2)
    assert status['scattered_target_status']=='completed_count_reached'
    assert not status['sampling_complete'] and not status['all_chips_found']
    assert status['measured_distinct_chip_count']==0
    unfinished=sampling_status(p,set(),[],measured_count=0,observed_count=0,completed_count=0,
        expected_count=2,unfinished_discoveries=1)
    assert not unfinished['sampling_complete'] and unfinished['remaining_boundary_attempts']==1


def test_scattered_continuation_exhausts_persisted_plan_and_reports_missing_count(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,oldsearch=saved_campaign
    options=replace(options,scattered_search=True,expected_chip_count=3)
    search=campaign.plan_campaign_search([0,100,0,100],options.minimum_chip_diameter_um,
        oldsearch['start_xy_um'],options.detectable_patch_diameter_um,options.max_seed_probes,scattered_search=True)
    state=json.loads((out/'batch.json').read_text());state['options']=asdict(options)
    atomic_json(out/'batch.json',state);atomic_json(out/'search-plan.json',search)
    scope=install_fake(monkeypatch,frame)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,
        continue_discovery=True,allow_source_change=True)
    assert result['state']=='needs_attention' and result['remaining_lattice_indices']==[]
    assert result['termination_reason']=='search_sampling_complete'
    assert result['expected_completed_chip_count_remaining']==3
    assert result['expected_chip_count']==3 and result['observed_distinct_chip_count']==0
    assert result['expected_chip_count_remaining']==3 and not result['all_chips_found']
    assert result['scattered_target_status']=='sampling_exhausted_target_unconfirmed'
    assert all(0<=x<=100 and 0<=y<=100 for x,y in scope.travels)
    assert sorted(r['index'] for r in result['seed_records'])==list(range(search['probe_count']))
    assert all(r['search_priority']['plan_sha256']==search['scattered_plan_sha256'] for r in result['seed_records'])
