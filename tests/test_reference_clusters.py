import numpy as np
import pytest
from flakefinder.reference_clusters import fit_reference_clusters,classify_pixels,profile_fingerprint


def samples():
    profile=dict(material='MoTe2',substrate='SiO2/Si',oxide_nm=285,objective='test20x',radiometry_id='test-linear-darkcorrected')
    rng=np.random.default_rng(4);identity=profile_fingerprint(profile);records=[]
    for label,mean in [('substrate',[0,0,0]),('confirmed_A',[.08,.13,-.02])]:
        for i,c in enumerate(np.array(mean)+rng.normal(0,.005,(8,3))):
            records.append(dict(flake_id=f'{label}-{i}',class_id=label,contrast_rgb=c.tolist(),profile_id=identity,
                source='synthetic fixture',reference_id=f'physical-{label}-{i}',confirmation_method='bare_substrate' if label=='substrate' else 'AFM',thickness_nm=.7))
    return profile,records


def test_background_material_and_outlier_are_distinct():
    p,r=samples();m=fit_reference_clusters(r,p)
    labels=classify_pixels(np.array([[0,0,0],[.08,.13,-.02],[.7,-.4,.7]]),m,profile_id=profile_fingerprint(p))
    assert labels['class_index'].tolist()==[0,1,-1]


def test_repeated_images_do_not_inflate_independent_flake_count():
    p,r=samples();few=[x for x in r if x['class_id']=='substrate' or x['flake_id'].endswith('-0')]
    with pytest.raises(ValueError,match='too few'):
        fit_reference_clusters(few*10,p)


def test_unconfirmed_training_and_mixed_profiles_refused():
    p,r=samples();r[-1]['confirmation_method']='optical_guess'
    with pytest.raises(ValueError,match='independent thickness'):
        fit_reference_clusters(r,p)
    p,r=samples();m=fit_reference_clusters(r,p)
    with pytest.raises(ValueError,match='profile differs'):
        classify_pixels(np.zeros((2,3)),m,profile_id='another-camera')
