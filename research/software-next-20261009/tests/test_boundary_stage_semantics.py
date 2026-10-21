"""Boundary closure needs measured stage, not arbitrary material colour mismatch."""
from pathlib import Path
import importlib.util
import numpy as np
import pytest
from flakefinder.model import Frame,Position,CameraTransform
from flakefinder.layout import SubstrateReference,SUBSTRATE,NON_SUBSTRATE,UNKNOWN
from flakefinder.stage_background import KEY,build_model
from flakefinder.chip_discovery import BoundaryOptions,classify_boundary


def frame(rgb):
    return Frame(np.asarray(rgb,dtype=np.uint16),Position(0.,0.,10.),(1.,1.),1,
        metadata={'white_level':4095,'camera_resources':{'ff_exposure_ms':200},'objective_position':2})


@pytest.fixture
def calibrated():
    bright=np.array([413,772,486]);dark=np.array([43,47,29])
    ref=frame(np.broadcast_to(bright,(100,200,3)).copy())
    raw=ref.rgb.copy();raw[:,100:]=dark
    evidence={'edge':{'normal':[1.,0.],'offset_um':0.},'raw_validation':{'accepted':True}}
    controls={'accepted':True,'bright_control_rgb':bright.tolist(),'dark_control_rgb':dark.tolist()}
    ref.metadata[KEY]=build_model(ref,frame(raw),evidence,CameraTransform(np.eye(2).tolist(),(1.,1.),True),
        controls,provenance={'edge_tiff_sha256':'a'*64,'edge_metadata_sha256':'b'*64})
    return ref,SubstrateReference.from_frame(ref),BoundaryOptions([-1000,1000,-1000,1000])


def test_color_mismatch_is_unknown_only_for_measured_boundary(calibrated):
    ref,model,options=calibrated
    image=ref.rgb.copy();image[:,100:]=[900,1000,300]  # bright bulk flake
    probe=frame(image);generic=model.classify(probe)
    assert np.all(generic[:,100:]==NON_SUBSTRATE)
    boundary=classify_boundary(probe,model,[413,772,486],options)
    assert np.all(boundary[:,100:]==UNKNOWN)
    assert np.array_equal(boundary==SUBSTRATE,generic==SUBSTRATE)
    assert np.all(model.classify(probe)[:,100:]==NON_SUBSTRATE)  # generic semantics unchanged


def test_measured_stage_dark_shadow_and_unrecognized_dark_are_distinct(calibrated):
    ref,model,options=calibrated
    image=ref.rgb.copy()
    image[:,:50]=[43,47,29]       # measured stage, below generic exposure gate
    image[:,50:100]=[33,61,38]   # substrate chromaticity in shadow
    image[:,100:150]=[8,50,8]    # dark does not establish stage identity
    labels=classify_boundary(frame(image),model,[413,772,486],options)
    assert np.all(labels[:,:50]==NON_SUBSTRATE)
    assert np.all(labels[:,50:150]==UNKNOWN)
    assert np.all(labels[:,150:]==SUBSTRATE)


def test_legacy_reference_keeps_original_boundary_behavior(calibrated):
    ref,_,options=calibrated
    del ref.metadata[KEY];legacy=SubstrateReference.from_frame(ref)
    image=ref.rgb.copy();image[:,:100]=[900,1000,300];image[:,100:]=[33,61,38]
    assert np.all(classify_boundary(frame(image),legacy,[413,772,486],options)==NON_SUBSTRATE)


def test_context_mismatch_still_rejected_before_boundary_classification(calibrated):
    ref,model,options=calibrated
    probe=frame(ref.rgb.copy());probe.metadata['objective_position']=3
    with pytest.raises(ValueError):classify_boundary(probe,model,[413,772,486],options)


def test_actual_hbn_false_flake_and_three_chip_sides(tmp_path):
    run=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy\data\scan-20261015-163303-8fcd7fa0')
    if not (run/'discovery/candidate001/discovery.json').exists():pytest.skip('Saved lab replay unavailable')
    script=Path(__file__).resolve().parents[2]/'chip-edge-20261015/replay.py'
    spec=importlib.util.spec_from_file_location('boundary_stage_replay',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    report=module.replay(run,tmp_path/'report.json')
    rows={r['probe']:r for r in report['rows']}
    assert all(r['substrate_unchanged'] for r in rows.values())
    assert rows[1]['boundary_labels']['stage']==0
    assert not rows[1]['primary_raw_edge_accepted']
    assert not rows[1]['rectangle_raw_accepted']
    assert not rows[1]['stored_edge_material_support']['accepted']
    assert rows[1]['local_strips']['supported_bins']==0
    for index in [9,12,19]:
        assert rows[index]['rectangle_raw_accepted']
        assert rows[index]['stored_edge_material_support']['accepted']
        assert rows[index]['local_strips']['supported_bins']>=8
        assert rows[index]['local_strips']['supported_span_um']>=200
        assert rows[index]['local_strips']['outside']['stage']>.85
    assert rows[9]['primary_raw_edge_accepted'] and rows[12]['primary_raw_edge_accepted']
