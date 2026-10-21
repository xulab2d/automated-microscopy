"""Staged filtering must preserve masks and distinguish review from identification."""
from dataclasses import asdict
import json
import numpy as np

from flakefinder.model import Frame,Position,CameraTransform
from flakefinder.staged_features import StagedFeatureOptions,_geometry,apply_sieves,_deduplicate_catalog


def region(area=200.,contrast=(.05,.08,.02)):
    return {'id':'one','chip_id':'chip001','tile_id':'tile_000000','area_um2':area,'usable_width_um':8.,
        'contrast_rgb':list(contrast),'touches_border':False,'stage_xy_um':[0.,0.],
        'polygon_px':[[0,0],[2,0],[2,2],[0,2]],'holes_px':[],'source_image':'tiles/tile_000000.tiff'}


def measured(contrast):
    return {'measurement_status':'measured','reason':'test local substrate',
        'contrast_rgb':list(contrast),'contrast_covariance':(np.eye(3)*1e-6).tolist()}


def test_size_and_color_sieves_leave_every_boundary_intact():
    for area,color,selected in [(20.,(.05,.08,.02),False),(200.,(0.,0.,0.),False),(200.,(.05,.08,.02),True)]:
        row=region(area,color);geometry=json.dumps({k:row[k] for k in ('polygon_px','holes_px','area_um2')})
        apply_sieves(row,measured(color),StagedFeatureOptions())
        assert row['selected_for_review'] is selected
        assert json.dumps({k:row[k] for k in ('polygon_px','holes_px','area_um2')})==geometry
        assert row['layer_number'] is None and not row['chemical_identity_confirmed']
    assert row['proposal_stage']=='thin_contrast_candidate'


def test_unavailable_substrate_measurement_does_not_delete_useful_feature():
    row=region();apply_sieves(row,{'measurement_status':'unmeasurable','reason':'insufficient_clear_local_substrate'},StagedFeatureOptions())
    assert row['selected_for_review'] and row['thin_reference_evidence']['compatible'] is None
    assert 'local_color_measurement_unavailable' in row['evidence_warnings']


def test_exact_geometry_preserves_holes_and_spatially_separate_objects():
    labels=np.zeros((50,60),np.int32);labels[5:30,5:30]=1;labels[12:20,12:20]=0;labels[6:16,42:52]=2
    original=labels.copy();frame=Frame(np.ones((50,60,3),np.uint16),Position(0,0,0),(1,1),0)
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    row=_geometry(labels,1,(slice(5,30),slice(5,30)),frame,transform)
    assert row['holes_px'] and row['mask_area_pixels']==25*25-8*8
    assert row['area_um2']==row['mask_area_pixels'] and np.array_equal(labels,original)
    other=_geometry(labels,2,(slice(6,16),slice(42,52)),frame,transform)
    assert other['area_um2']==100 and other['stage_xy_um']!=row['stage_xy_um']


def test_overlap_catalog_dedup_does_not_change_source_observations():
    rows=[]
    for i,x in enumerate([0.,.1,40.]):
        row=region();row.update(id=str(i),tile_id=f'tile_{i:06d}',stage_xy_um=[x,0.],label='optical feature')
        rows.append(row)
    before=json.dumps(rows,sort_keys=True);catalog=_deduplicate_catalog(rows)
    assert len(catalog)==2 and json.dumps(rows,sort_keys=True)==before
    assert catalog[0]['overlap_observations'][0]['id']=='1'
