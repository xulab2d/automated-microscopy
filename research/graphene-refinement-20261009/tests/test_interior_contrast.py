import numpy as np
import pytest

from flakefinder.material_contrast import MaterialContrastClassifier
from flakefinder.material_terraces import MaterialTerraceOptions, segment_material_terraces


def classify(image, parents, pixel=(1., 1.), trim=2.):
    model = {'kind': 'empirical_material_populations', 'components': [
        {'label': 'synthetic-material', 'role': 'material',
         'mean_rgb': [.15, .1, .05], 'covariance_rgb': (np.eye(3)*.02**2).tolist()}]}
    return segment_material_terraces(image, np.ones(parents.shape, bool), parents==0,
        np.eye(3)*.001**2, pixel, MaterialContrastClassifier(model), parent_labels=parents,
        options=MaterialTerraceOptions(smoothing_um=0, interior_erosion_um=trim))


def scene():
    image = np.random.default_rng(6).normal(0, .001, (100, 150, 3)).astype(np.float32)
    parents = np.zeros(image.shape[:2], np.int32)
    return image, parents


def test_blurred_rim_does_not_bias_interior_color_or_shrink_saved_region():
    image, parents = scene()
    parents[20:60, 30:80] = 1
    image[20:60, 30:80] = [.12, .08, .04]
    image[22:58, 32:78] = [.15, .1, .05]
    result = classify(image, parents)
    row, = result['regions']
    assert row['area_px'] == 2000
    assert np.array_equal(result['labels']>0, parents>0)
    assert row['measurement']['interior_pixels'] == 36*46
    assert row['measurement']['contrast_basis'] == 'eroded_interior'
    assert row['contrast_rgb'] == pytest.approx([.15, .1, .05], abs=.0001)
    # Whole-mask radiometry is measurably biased despite identical segmentation.
    whole = classify(image, parents, trim=0)['regions'][0]
    assert whole['contrast_rgb'][0] < .146
    assert row['selected_for_review']


def test_component_crop_keeps_global_coordinates_holes_and_separate_islands():
    image, parents = scene()
    parents[20:45, 15:45] = 7
    parents[65:90, 105:140] = 7
    parents[28:34, 26:32] = 0
    image[parents>0] = [.15, .1, .05]
    result = classify(image, parents)
    assert np.array_equal(result['parent_labels'], parents)
    assert np.array_equal(result['labels']>0, parents>0)
    assert len(result['regions']) == 2
    assert result['labels'][22, 20] != result['labels'][70, 110]
    assert any(row['holes_px'] for row in result['regions'])
    for row in result['regions']:
        x, y, w, h = row['usable_rectangle']['bbox_px']
        assert w > 0 and h > 0
        assert np.all(result['labels'][y:y+h, x:x+w] == row['instance_label'])
        for px, py in row['polygon_px']:
            assert result['labels'][py, px] == row['instance_label']


def test_no_interior_is_retained_but_cannot_qualify_from_blurred_color():
    image, parents = scene()
    parents[40, 30:100] = 1
    image[parents>0] = [.15, .1, .05]
    result = classify(image, parents)
    row, = result['regions']
    assert row['area_px'] == 70 and np.count_nonzero(result['labels']) == 70
    assert row['measurement']['interior_pixels'] == 0
    assert row['measurement']['contrast_basis'] == 'whole_region_diagnostic_only'
    assert not row['selected_for_review']
    assert 'no_blur_free_interior' in row['filter_reasons']


def test_physical_trim_respects_anisotropic_pixel_calibration():
    image, parents = scene()
    parents[20:60, 30:80] = 1
    image[parents>0] = [.15, .1, .05]
    row, = classify(image, parents, pixel=(.5, 1.))['regions']
    assert row['measurement']['interior_pixels'] == (50-8)*(40-4)
    assert row['area_um2'] == 1000
