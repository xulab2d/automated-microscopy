"""Fresh rectangle discovery and truthful exhaustion, without hardware."""
from dataclasses import replace
import json
import math

import pytest

from flakefinder import chip_campaign as campaign
from flakefinder.model import SafetyError
from flakefinder.sparse_acquisition import rectangle_work_budget
from test_chip_campaign_continuation import saved_campaign, install_fake


def test_rectangle_uses_whole_clipped_area_and_memory_bounded_evidence():
    bounds=[31.,84999.,31.,50000.];seed=[12000.,12000.]
    options=campaign.CampaignOptions(5000)
    edge=campaign.boundary_search_options(seed,bounds,options,19079.,rectangle_mode=True)
    assert edge.search_bounds_um==bounds and edge.rectangle_mode
    assert edge.search_bounds_um[1]-seed[0]>options.boundary_radius_um
    assert math.ceil((bounds[1]-bounds[0])/edge.cell_um)*math.ceil((bounds[3]-bounds[2])/edge.cell_um)<=12000000
    budget=rectangle_work_budget(edge,seed,remaining_seconds=1234.,field_um=[420,350])
    assert budget['maximum_total_geometry_frames']>32
    assert budget['max_elapsed_s']==1234.
    legacy=campaign.boundary_search_options(seed,bounds,options,19079.)
    assert not legacy.rectangle_mode and legacy.search_bounds_um==[3000.,21000.,3000.,21000.]
    assert legacy.cell_um==10.


def test_fresh_rectangle_requires_no_handoff_but_continuation_still_does(saved_campaign,monkeypatch):
    cfg,reference,old,options,frame,transform,search=saved_campaign
    out=old.parent/'fresh-rectangle'
    options=replace(options,scattered_search=True,expected_chip_count=1)
    scope=install_fake(monkeypatch,frame)
    seen=[]
    def classify(*args):
        return {'classification':'substrate_seed' if not seen else 'non_substrate_sample'}
    def discovery(scope,xy,transform,reference,folder,edge,**kwargs):
        seen.append((edge,kwargs))
        assert edge.rectangle_mode and edge.search_bounds_um==[0.,100.,0.,100.]
        assert 0<kwargs['max_elapsed_s']<=options.walltime_budget_s
        return {'state':'unresolved','reason':'Unbracketed edge','records':[]}
    monkeypatch.setattr(campaign,'_seed_classification',classify)
    monkeypatch.setattr(campaign,'discover_sparse_outline',discovery)
    monkeypatch.setattr(campaign,'interior_reseed',lambda *a,**k:None)
    result=campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,rectangle_mode=True)
    assert len(seen)==1 and scope.closed
    assert result['state']=='needs_attention' and result['termination_reason']=='search_sampling_complete'
    assert result['expected_completed_chip_count_remaining']==1 and result['discovery_unresolved_count']==1
    assert result['rectangle_mode'] and not result['all_chips_found']
    assert not (out/'rectangle-handoff-proof.json').exists()
    saved=json.loads((out/'batch.json').read_text())
    assert saved['boundary_attempts'][0]['boundary_search_policy']['bounds_policy']=='operator_search_envelope'
    with pytest.raises(SafetyError,match='handoff'):
        campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,rectangle_mode=True,
            continue_discovery=True,allow_source_change=True)


@pytest.mark.parametrize('state,expected,attention,remaining',[
    ({'chip_states':{}},1,True,1),
    ({'chip_states':{}},None,True,None),
    ({'chip_states':{'a':{'planned_area_acquired':False}}},1,True,1),
    ({'chip_states':{'a':{'planned_area_acquired':True}}},1,False,0),
    ({'chip_states':{'a':{'planned_area_acquired':True}},'boundary_attempts':[{'state':'unresolved'}]},1,False,0),
    ({'chip_states':{'a':{'planned_area_acquired':True}},'boundary_attempts':[{'state':'unresolved'}]},None,True,None),
    ({'chip_states':{'a':{'planned_area_acquired':True}},'boundary_attempts':[{'state':'duplicate_observed_rectangle'}]},1,False,0),
])
def test_terminal_summary_counts_acquired_not_predicted_chips(state,expected,attention,remaining):
    result=campaign.campaign_terminal_summary(state,expected)
    assert result['requires_attention'] is attention
    assert result['expected_completed_chip_count_remaining']==remaining
    assert 'all_chips_found' not in result
    if not attention and state.get('boundary_attempts',[{}])[0].get('state')=='unresolved':
        assert result['completion_warnings'] and not result['completion_reasons']
