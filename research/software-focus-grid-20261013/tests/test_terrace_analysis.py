from dataclasses import asdict
from pathlib import Path
import json
import hashlib
import cv2

import numpy as np
import pytest

from flakefinder.model import CameraTransform,Frame,Position,AcquisitionError
from flakefinder.terrace_analysis import (TerraceOptions,largest_uniform_rectangle,segment_plateaus,
    analyze_terrace_frame)
from flakefinder.terrace_domain import ChipDomain,digest,normalize_supported
from flakefinder.terrace_parents import load_parent_field
from flakefinder.terrace_substrate import recorded_profile,check_substrate_consistency,_summarize


def fixture_frame(contrast,pixel=.25):
    rgb=np.uint16(np.clip(2000*(1-contrast),0,4095))
    return Frame(rgb,Position(0,0,0),(pixel,pixel),0,metadata={'white_level':4095})


def test_touching_different_contrast_plateaus_keep_one_parent_and_separate_children():
    rng=np.random.default_rng(41)
    contrast=rng.normal(0,.001,(180,240,3)).astype('float32')
    thin=np.zeros(contrast.shape[:2],bool);thin[50:130,50:90]=True
    bulk=np.zeros_like(thin);bulk[50:130,90:150]=True
    contrast[thin]+=[.03,.055,.01];contrast[bulk]+=[-.5,-.6,-.3]
    frame=fixture_frame(contrast);valid=np.ones(thin.shape,bool);support=~(thin|bulk)
    labels,parents,evidence,_=segment_plateaus(frame,contrast,valid,support)
    thin_label=int(np.bincount(labels[thin]).argmax());bulk_label=int(np.bincount(labels[bulk]).argmax())
    assert thin_label>0 and bulk_label>0 and thin_label!=bulk_label
    assert evidence[thin_label]['parent_instance_label']==evidence[bulk_label]['parent_instance_label']
    assert np.mean(labels[thin]==thin_label)>.8
    assert np.mean(labels[bulk]==bulk_label)>.8
    assert evidence[thin_label]['parent_area_um2']>thin.sum()*.25**2


def test_zero_contrast_does_not_create_or_pass_a_region():
    c=np.zeros((120,160,3),np.float32);f=fixture_frame(c)
    labels,parents,evidence,_=segment_plateaus(f,c,np.ones(c.shape[:2],bool),np.ones(c.shape[:2],bool))
    assert not labels.any() and not parents.any() and not evidence


def test_optical_pixel_classes_precede_grouping_and_mismatch_is_retained():
    rng=np.random.default_rng(41);c=rng.normal(0,.001,(180,240,3)).astype('float32')
    c[50:130,50:90]+=[.03,.055,.01];c[50:130,90:150]+=[-.5,-.6,-.3]
    support=np.ones(c.shape[:2],bool);support[45:135,45:155]=False
    class Provider:
        provenance={'scope':'synthetic pre-grouping test'}
        called=0
        def classify_pixels(self,values,covariance):
            self.called+=len(values)
            return {'min_mahalanobis_distance':np.where(values[:,0]>0,.5,100.),
                'best_conditional_thickness_nm':np.full(len(values),1.5)}
    provider=Provider();labels,parents,rows,prepared=segment_plateaus(fixture_frame(c),c,np.ones_like(support),support,optical_provider=provider)
    assert provider.called>1000
    thin=int(np.bincount(labels[60:120,60:80].ravel()).argmax());bulk=int(np.bincount(labels[60:120,100:140].ravel()).argmax())
    assert rows[thin]['pixel_classification']=='conditional_optical_thickness_bin'
    assert rows[thin]['conditional_thickness_bin_nm']==[1.,2.]
    assert rows[bulk]['pixel_classification']=='unclassified_contrast_population'
    assert thin!=bulk and parents[80,70]==parents[80,120]
    assert np.isscalar(rows[thin]['parent_area_um2'])
    assert rows[thin]['parent_area_um2']==rows[bulk]['parent_area_um2']
    assert np.isfinite(prepared['optical_maps']['best_conditional_thickness_nm'][80,70])


def test_largest_rectangle_is_inside_uniform_mask_and_holes_with_anisotropic_scale():
    mask=np.zeros((16,20),bool);mask[2:14,2:18]=True;mask[2:10,9:12]=False
    got=largest_uniform_rectangle(mask,(.2,.4),(100,200))
    x,y,w,h=got['bbox_px'];local=mask[y-200:y-200+h,x-100:x-100+w]
    assert local.all() and local.size==w*h
    # Independent brute-force area oracle on this small fixture.
    maximum=0
    for y0 in range(mask.shape[0]):
        for y1 in range(y0+1,mask.shape[0]+1):
            columns=mask[y0:y1].all(axis=0);run=0
            for value in columns:
                run=run+1 if value else 0;maximum=max(maximum,run*(y1-y0))
    assert got['area_um2']==pytest.approx(maximum*.08)
    assert got['width_um']==pytest.approx(w*.2) and got['height_um']==pytest.approx(h*.4)
    assert largest_uniform_rectangle(np.zeros((5,5),bool))['area_um2']==0


def test_accepted_parent_boundaries_and_holes_are_exact_while_children_stay_inside():
    rng=np.random.default_rng(11);c=rng.normal(0,.001,(160,200,3)).astype('float32')
    parents=np.zeros(c.shape[:2],np.int32);parents[30:130,40:150]=71;parents[60:75,80:95]=0
    c[parents==71]+=[.1,.16,.04]
    c[:,155:]+=[-.4,-.5,-.2] # Strong outside-parent feature cannot leak into children.
    labels,kept,rows,_=segment_plateaus(fixture_frame(c),c,np.ones(c.shape[:2],bool),parents==0,parent_labels=parents)
    assert np.array_equal(kept,parents)
    assert np.all(parents[labels>0]==71)
    assert rows and all(r['parent_instance_label']==71 for r in rows.values())


def test_parent_source_checksum_and_relative_path_are_enforced(tmp_path):
    f=fixture_frame(np.zeros((120,160,3),np.float32));labels=np.zeros((120,160),np.uint16);labels[20:80,30:90]=17
    cv2.imwrite(str(tmp_path/'mask.png'),labels)
    tile={'key':'c/tile_000000','observations_file':'field.json','instance_mask':'mask.png',
        'sha256':hashlib.sha256(f.rgb.tobytes()).hexdigest()}
    source={'.tiff':'source-tiff','.json':'source-sidecar'}
    shard={'tile':tile,'rows':[{'id':'retained-id','instance_label':17}],
        'provenance':{'source_files_sha256':source},'mask_sha256':digest(tmp_path/'mask.png')}
    (tmp_path/'analysis.json').write_text(json.dumps({'tiles':[tile]}));(tmp_path/'field.json').write_text(json.dumps(shard))
    item={'key':tile['key'],'source_hashes':source}
    loaded,rows,_=load_parent_field(tmp_path,item,f)
    assert np.array_equal(loaded,labels) and rows[0]['id']=='retained-id'
    labels[30,40]=0;cv2.imwrite(str(tmp_path/'mask.png'),labels)
    with pytest.raises(AcquisitionError,match='checksum'):load_parent_field(tmp_path,item,f)
    tile['observations_file']='../outside.json';(tmp_path/'analysis.json').write_text(json.dumps({'tiles':[tile]}))
    with pytest.raises(AcquisitionError,match='escapes'):load_parent_field(tmp_path,item,f)


def test_chip_substrate_qc_flags_mismatch_support_and_profiles_without_mutating_pixels():
    metadata={'white_level':4095,'camera_resources':{'ff_exposure_ms':200,'ff_white_balance':'1,1,1'},
        'objective':{'name':'20x','na':.4},'illumination_percent':90,'pixel_type':5}
    rgb=np.zeros((120,160,3),np.uint16)+[2000,2000,1600]
    frame=Frame(rgb,Position(0,0,0),(.25,.25),0,metadata=metadata)
    profile_id,_,missing=recorded_profile(metadata,frame.pixel_size_um);assert not missing
    measurement=_summarize(rgb.reshape(-1,3)[::10],4095)
    reference={'status':'ready','profile_id':profile_id,'reference_id':'saved-reference',
        'records':[{'tile_id':'ref'+str(i),'measurement':measurement} for i in range(6)]}
    before=frame.rgb.copy();support=np.ones(rgb.shape[:2],bool)
    result=check_substrate_consistency(reference,frame,support,'ref0')
    assert result['status']=='consistent_with_chip_reference' and result['score']==0
    assert result['leave_one_field_out'] and 'ref0' not in result['reference_sample_ids']
    assert np.array_equal(frame.rgb,before) and result['feature_masks_changed'] is False
    frame.rgb[:,:,0]=800
    assert check_substrate_consistency(reference,frame,support,'unseen')['status']=='gross_substrate_mismatch'
    support[:]=False;support[10:13,10:13]=True
    assert check_substrate_consistency(reference,frame,support,'unseen')['status']=='insufficient_substrate_samples'
    frame.metadata={**metadata,'illumination_percent':40}
    assert check_substrate_consistency(reference,frame,np.ones_like(support),'unseen')['status']=='reference_unavailable_for_optical_profile'


def test_normalization_requires_substrate_and_ignores_off_domain_extrapolation():
    y,x=np.mgrid[:160,:240];background=2000+2*x+y
    image=np.stack([background,background*1.1,background*.8],axis=-1)
    f=Frame(np.uint16(image),Position(0,0,0),(.25,.25),0)
    valid=np.zeros((160,240),bool);valid[:,100:]=True
    contrast,report=normalize_supported(f,valid,valid)
    assert np.max(abs(contrast[valid]))<.002 and np.all(contrast[~valid]==0)
    assert report['fit_support_independent_of_feature_color']
    with pytest.raises(AcquisitionError,match='substrate'):
        normalize_supported(f,valid,np.zeros_like(valid))


def test_domain_margin_is_chip_margin_not_a_camera_border(tmp_path):
    c=np.zeros((100,120,3),np.float32);frame=fixture_frame(c)
    labels=tmp_path/'boundary-labels/tile_000000.npz';labels.parent.mkdir()
    np.savez_compressed(labels,labels=np.ones((50,60),np.uint8))
    completed={'tile_000000':{'hashes':{'boundary-labels/'+labels.name:digest(labels)}}}
    manifest={'completed':completed}
    domain=ChipDomain(tmp_path,manifest,np.array([[-100,-100],[100,-100],[100,100],[-100,100]]),'observed_enclosed',{})
    item={'tile_id':'tile_000000','transform':CameraTransform([[.25,0],[0,.25]],(.25,.25),True)}
    valid,support,_=domain.masks(item,frame,12.)
    assert valid.all() # Camera border is well inside the physical chip.
    assert support.any()


def test_measurement_unavailable_cannot_be_a_selected_plateau():
    rng=np.random.default_rng(55);c=rng.normal(0,.001,(200,200,3)).astype('float32')
    c[45:155,45:155]+=[.08,.12,.03];frame=fixture_frame(c)
    class Domain:
        geometry_status='observed_enclosed'
        def masks(self,*args):
            # Only a remote corner is recorded substrate; no valid local annulus.
            support=np.zeros((200,200),bool);support[:10,:]=True;support[-10:,:]=True
            return np.ones(support.shape,bool),support,{}
    manifest={'completed':{'tile_000000':{'focus':{'success':True}}}}
    item={'key':'c/tile_000000','chip_id':'c','tile_id':'tile_000000','manifest':manifest,
        'transform':CameraTransform([[.25,0],[0,.25]],(.25,.25),True)}
    rows,maps,report=analyze_terrace_frame(item,frame,Domain())
    assert rows and all(not r['selected_for_review'] for r in rows)
    assert any('local_substrate_measurement_unavailable' in r['filter_reasons'] for r in rows)
    assert all(r['layer_number'] is None and not r['chemical_identity_confirmed'] for r in rows)
    assert maps['instances'].any()
