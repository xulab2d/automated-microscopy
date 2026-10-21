from copy import deepcopy
import numpy as np
import pytest
from flakefinder.model import Frame,Position,CameraTransform,SafetyError
from flakefinder.layout import SubstrateReference,SUBSTRATE,NON_SUBSTRATE,UNKNOWN
from flakefinder.stage_background import KEY,build_model,validate_model,pixel_hash
from flakefinder.chip_discovery import BoundaryOptions,classify_boundary


def frame(rgb):
    return Frame(np.asarray(rgb,dtype=np.uint16),Position(0.,0.,10.),(1.,1.),1,
        metadata={'white_level':4095,'camera_resources':{'ff_exposure_ms':200},'objective_position':2})


@pytest.fixture
def measured():
    rng=np.random.default_rng(55)
    bright=np.array([413.,772.,486.]);dark=np.array([43.,47.,29.])
    ref=frame(np.clip(bright+rng.normal(0,2,(200,400,3)),0,4095))
    rgb=ref.rgb.copy()
    rgb[:,200:]=np.maximum(5,dark+rng.normal(0,[14,7,5],(200,200,3)))
    edge=frame(rgb)
    evidence={'edge':{'normal':[1.,0.],'offset_um':0.},'raw_validation':{'accepted':True}}
    controls={'accepted':True,'bright_control_rgb':bright.tolist(),'dark_control_rgb':dark.tolist()}
    transform=CameraTransform([[1.,0.],[0.,1.]],(1.,1.),True)
    model=build_model(ref,edge,evidence,transform,controls,provenance={
        'edge_tiff_path':'fixture','edge_tiff_sha256':'a'*64,'edge_metadata_sha256':'b'*64})
    return ref,edge,evidence,controls,transform,model


def constant(rgb):return frame(np.broadcast_to(rgb,(200,400,3)).copy())


def test_stage_color_below_old_exposure_gate_is_measured_not_blind_dark(measured):
    ref,_,_,_,_,model=measured
    old=SubstrateReference.from_frame(ref)
    stage=constant([100,123,60])
    assert np.mean(old.classify(stage)==UNKNOWN)==1.
    ref.metadata[KEY]=model;new=SubstrateReference.from_frame(ref)
    assert np.mean(new.classify(stage)==NON_SUBSTRATE)>.99
    assert np.mean(new.classify(constant([0,0,0]))==UNKNOWN)==1.
    assert np.mean(new.classify(constant([5,40,5]))==UNKNOWN)==1.


def test_shadowed_substrate_chromaticity_cannot_become_stage(measured):
    ref,_,_,_,_,model=measured;ref.metadata[KEY]=model
    new=SubstrateReference.from_frame(ref)
    shadow=constant(np.array([413.,772.,486.])*.08)
    assert np.mean(new.classify(shadow)==NON_SUBSTRATE)==0.
    opts=BoundaryOptions((-1000,1000,-1000,1000))
    # The old fixed15% test would force this entire shadow into holder.
    assert np.mean(classify_boundary(shadow,new,[413,772,486],opts)==NON_SUBSTRATE)==0.
    assert np.mean(new.classify(ref)==SUBSTRATE)>.99


def test_mixed_boundary_retains_both_sides(measured):
    ref,_,_,_,_,model=measured;ref.metadata[KEY]=model
    rgb=ref.rgb.copy();rgb[:,200:]=[100,123,60]
    labels=SubstrateReference.from_frame(ref).classify(frame(rgb))
    assert np.mean(labels[:,:200]==SUBSTRATE)>.99
    assert np.mean(labels[:,200:]==NON_SUBSTRATE)>.99


@pytest.mark.parametrize('change',['pixels','context','scale','parameters'])
def test_measured_model_is_bound_to_reference_pixels_context_and_parameters(measured,change):
    ref,_,_,_,_,model=measured;ref.metadata[KEY]=deepcopy(model)
    if change=='pixels':ref.rgb[0,0,0]+=1
    elif change=='context':ref.metadata['camera_resources']['ff_exposure_ms']=100
    elif change=='scale':ref.pixel_size_um=(2.,2.)
    else:ref.metadata[KEY]['rgb_upper'][0]+=10
    with pytest.raises(ValueError):SubstrateReference.from_frame(ref)


def test_rejected_raw_edge_or_wrong_exposure_cannot_calibrate(measured):
    ref,edge,evidence,controls,transform,model=measured
    evidence['raw_validation']['accepted']=False
    with pytest.raises(SafetyError):build_model(ref,edge,evidence,transform,controls,provenance=model['provenance'])
    evidence['raw_validation']['accepted']=True;edge.metadata['camera_resources']['ff_exposure_ms']=150
    with pytest.raises(SafetyError):build_model(ref,edge,evidence,transform,controls,provenance=model['provenance'])


def test_saved_reference_reload_retains_model_and_legacy_without_it_is_unchanged(measured,tmp_path):
    from flakefinder.imaging import save_frame,load_frame
    ref,_,_,_,_,model=measured
    legacy=SubstrateReference.from_frame(ref)
    assert 'measured_stage_background' not in legacy.to_dict()
    ref.metadata[KEY]=model
    save_frame(tmp_path,'reference',ref)
    loaded=load_frame(tmp_path/'reference.tiff')
    assert validate_model(loaded)['model_sha256']==model['model_sha256']
    assert pixel_hash(loaded)==pixel_hash(ref)
    opts=BoundaryOptions((-1000,1000,-1000,1000))
    shadow=constant(np.array([413.,772.,486.])*.08)
    assert np.mean(classify_boundary(shadow,legacy,[413,772,486],opts)==NON_SUBSTRATE)==1.


def test_saved_hbn_probe24_and_raw_edge_replay_when_available():
    """Actual prior failure, read-only; portable environments skip absent lab data."""
    import json
    from pathlib import Path
    from flakefinder.imaging import load_frame
    from flakefinder.vendor_lifecycle import file_hash
    from flakefinder.autonomous_reference import verify_reference
    from flakefinder.photometric_edges import validate_photometric_edge
    from flakefinder.sparse_edges import validated_coherent_edge
    run=Path(r'G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy\data\scan-20261015-145135-db809db3')
    if not (run/'autonomous-reference/proof.json').exists():pytest.skip('Saved lab hBN replay unavailable')
    proof=json.loads((run/'autonomous-reference/proof.json').read_bytes())
    discovery=json.loads((run/'discovery/candidate001/discovery.json').read_bytes())
    ref=load_frame(run/'calibration/substrate.tiff')
    original=SubstrateReference.from_frame(ref)
    edge_path=Path(proof['focused_edge_path']);edge=load_frame(edge_path)
    transform=CameraTransform(**discovery['transform'])
    assert file_hash(edge_path)==proof['focused_edge_sha256']
    assert file_hash(edge_path.with_suffix('.json'))==proof['focused_edge_metadata_sha256']
    raw=validate_photometric_edge(edge,proof['edge_evidence']['edge'],transform.matrix)
    assert raw['accepted']
    control=verify_reference(ref,edge,proof['edge_evidence'],transform)
    ref.metadata[KEY]=build_model(ref,edge,{**proof['edge_evidence'],'raw_validation':raw},transform,control,
        provenance={'edge_tiff_sha256':proof['focused_edge_sha256'],
                    'edge_metadata_sha256':proof['focused_edge_metadata_sha256']})
    measured=SubstrateReference.from_frame(ref)
    probe=load_frame(run/'discovery/candidate001/frames/probe_000024.tiff')
    assert np.mean(original.classify(probe)==UNKNOWN)>.99
    assert np.mean(measured.classify(probe)==NON_SUBSTRATE)>.99
    assert np.array_equal(measured.classify(ref)==SUBSTRATE,original.classify(ref)==SUBSTRATE)
    labels=measured.classify(edge)
    found,report=validated_coherent_edge(edge,labels,transform.matrix,discovery['edge_validation_profile'])
    assert found is not None and report['accepted']
