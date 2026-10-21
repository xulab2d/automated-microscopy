import hashlib
import json
import numpy as np
import pytest
from flakefinder.graphene_thickness import estimate_thickness,ThicknessReview


def ensemble():
    return {'thickness_nm':[0,.34,.68,1.02],'layer_spacing_nm':.34,
        'contrast_rgb':[[[0,0,0],[.1,.08,.05],[.2,.16,.1],[.3,.24,.15]],
                        [[0,0,0],[.08,.064,.04],[.16,.128,.08],[.24,.192,.12]]],
        'engineering_rgb_sigma':.005,'absolute_match_distance2':11.345}


def test_known_forward_point_recovers_equivalent_layers_and_sensitivity():
    result=estimate_thickness([.2,.16,.1],np.eye(3)*1e-6,ensemble())
    assert result['provisional_layer_estimate']==2
    assert result['status']=='conditional_optical_fit'
    assert result['rgb_residual']==[0,0,0]
    assert result['compatible_layer_intervals']==[[2.,2.]]
    assert result['layer_assignment_confirmed'] is False


def test_mismatch_not_misreported_as_confident_count_and_grid_edge_explicit():
    result=estimate_thickness([.2,.4,.01],np.zeros((3,3)),ensemble())
    assert result['status']=='model_mismatch' and result['compatible_layer_intervals']==[]
    assert result['provisional_layer_estimate'] is not None
    edge=estimate_thickness([.3,.24,.15],np.zeros((3,3)),ensemble())
    assert edge['grid_boundary'] and edge['status']=='grid_boundary'
    with pytest.raises(ValueError):estimate_thickness([.1,.2,.3],-np.eye(3),ensemble())


def test_nonmonotonic_curve_keeps_disconnected_compatible_intervals():
    model=ensemble();model['contrast_rgb']=[[[0,0,0],[.1,.08,.05],[.2,.16,.1],[.1,.08,.05]]]
    result=estimate_thickness([.1,.08,.05],np.zeros((3,3)),model)
    assert result['compatible_layer_intervals']==[[1.,1.],[3.,3.]]


def test_linear_assumption_separate_from_best_sensitivity_scenario():
    model=ensemble();model['scenario_provenance']=[{'gamma':.8},{'gamma':1.}]
    result=estimate_thickness([.2,.16,.1],np.eye(3)*1e-6,model)
    assert result['best_scenario']=={'gamma':.8}
    assert result['linear_response_layer_estimate'] in (2,3)
    assert result['linear_response_best_distance2']>result['best_distance2']


def test_lazy_detail_pins_selection_source_and_sidecar(tmp_path):
    def save(path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        data=json.dumps(value).encode();path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    run='scan-20261013-221727-131d9ced';folder=tmp_path/'data'/run
    output=tmp_path/'analysis/graphene'/run/'chip001/example';field='chip001/tile_000001';identifier=field+'/1'
    plan_sha=save(output/'plan.json',{'model_sha256':'model'})
    save(folder/'graphene-analysis-selection.json',{'analysis_relative_path':output.relative_to(tmp_path).as_posix(),'plan_sha256':plan_sha})
    row={'instance_label':1,'contrast_rgb':[.2,.16,.1],'contrast_covariance':np.eye(3).tolist(),
        'measurement':{'interior_measurement_available':True}}
    source_sha=save(output/'field.json',{'field':field,'regions':[row]})
    bundle={'schema':1,'run_id':run,'chip':'chip001','source_plan_sha256':plan_sha,'source_model_sha256':'model',
        'source_fields':{field:{'path':'field.json','sha256':source_sha}},'regions':{},'ensemble':ensemble(),'assumptions':{}}
    sidecar=output.parent/'thickness-v1/thickness.json';sha=save(sidecar,bundle)
    pointer=folder/'graphene-thickness-selection-chip001.json'
    selector={'run_id':run,'source_plan_sha256':plan_sha,'path':sidecar.relative_to(tmp_path).as_posix(),'sha256':sha}
    reader=ThicknessReview(tmp_path)
    assert reader.data(run,identifier)['status']=='not_available'
    save(pointer,selector)
    result=reader.data(run,identifier)
    assert result['observation']==identifier and result['provisional_layer_estimate']==2
    assert reader.data(run,identifier)==result
    save(output/'field.json',{'field':field,'regions':[]})
    with pytest.raises(ValueError,match='checksum'):reader.data(run,identifier)
    save(pointer,{**selector,'source_plan_sha256':'wrong'})
    with pytest.raises(ValueError,match='current analysis'):reader.data(run,identifier)
    with pytest.raises(ValueError):reader.data('../bad',identifier)
