import cv2
import numpy as np

from flakefinder.analysis import empirical_proposals,extract_regions
from flakefinder.colors import AnalysisOptions,ColorGroups,prepare_contrast
from flakefinder.contrast_reference import bulk_reference_labels
from flakefinder.model import Frame,Position


def measure(image,pixel=.5):
    frame=Frame(np.uint16(image),Position(0,0,0),(pixel,pixel),0)
    options=AnalysisOptions(max_components=6)
    prepared=prepare_contrast(frame,options)
    groups=ColorGroups({'groups':[]})
    rows,masks=extract_regions(frame,prepared,groups,options,coherent=True)
    return empirical_proposals(rows,options),masks


def test_faint_large_terrace_survives_and_keeps_interior_hole():
    rng=np.random.default_rng(81)
    image=np.full((240,300,3),2000.,float)
    target=np.zeros((240,300),bool);target[70:145,90:180]=True;target[95:110,120:140]=False
    image[target]*=[.988,.98,.99];image+=rng.normal(0,1,image.shape)
    rows,masks=measure(image)
    accepted=[r for r in rows if r['disposition']=='candidate']
    assert accepted
    best=max(accepted,key=lambda r:r['area_um2']);got=masks==best['instance_label']
    assert np.count_nonzero(got&target)/np.count_nonzero(got|target)>.85
    assert best['holes_px'] and not got[102,130]
    assert abs(best['area_um2']-got.sum()*.25)<1e-8


def test_distinct_adjacent_terraces_do_not_pool_their_colors_or_areas():
    image=np.full((250,320,3),2000.,float)
    image[75:155,80:125]*=[.92,.9,.96]
    image[75:155,125:180]*=[.77,.68,.85]
    rows,masks=measure(image)
    accepted=[r for r in rows if r['disposition']=='candidate']
    assert len(accepted)==2
    assert len({int(masks[110,100]),int(masks[110,150])})==2
    assert all(r['area_um2']<1200 for r in accepted)


def test_whole_image_bulk_reference_finds_disconnected_objects_and_rejects_dark_dust():
    image=np.full((260,360,3),2000.,float)
    image[60:110,55:110]*=[1.75,1.65,1.8]
    # The user selected red/green only; blue can differ without an invented veto.
    image[125:180,190:245]*=[1.78,1.7,1.95]
    image[45:95,235:300]*=.7
    frame=Frame(np.uint16(image),Position(0,0,0),(.5,.5),0)
    options=AnalysisOptions();prepared=prepare_contrast(frame,options)
    labels,evidence=bulk_reference_labels(frame,prepared,options)
    assert len(evidence)==2 and labels[80,80]>0 and labels[150,220]>0
    assert labels[80,80]!=labels[150,220] and labels[70,260]==0
    assert all(r['reference_kind'].startswith('user-identified bulk') for r in evidence.values())


def test_catalog_cutoff_and_overlap_do_not_erase_source_observations(tmp_path):
    from dataclasses import asdict
    import json
    from flakefinder.analysis import analyze_scan
    from flakefinder.imaging import save_frame
    from flakefinder.model import CameraTransform,atomic_json
    image=np.full((130,180,3),2000.,float)
    image[35:80,55:95]*=[1.75,1.65,1.8]
    image[90:96,120:126]*=[1.75,1.65,1.8]
    transform=CameraTransform([[.5,0],[0,.5]],(.5,.5),True);completed={}
    for i in range(2):
        name=f'tile_{i:06d}';frame=Frame(np.uint16(image),Position(1000,2000,0),(.5,.5),i)
        save_frame(tmp_path/'tiles',name,frame);completed[name]={'position':asdict(frame.position)}
    atomic_json(tmp_path/'manifest.json',{'transform':asdict(transform),'plan':{'chip_id':'control'},
        'context':{'material':'MoTe2'},'completed':completed})
    summary=analyze_scan(tmp_path,{'backend':'bulk_reference'})
    observations=json.loads((tmp_path/'analysis/observations.json').read_text())['observations']
    assert len(observations)==4 and summary['candidate_count']==1
    assert summary['below_catalog_size_count']==2 and summary['thin_screen_count']==0
    assert sum(r['catalog_id'] is not None for r in observations)==2
    assert all(r['review_role']=='bulk_positive_control' and r['layer_number'] is None for r in observations)
    for tile in summary['tiles']:
        masks=cv2.imread(str(tmp_path/'analysis'/tile['instance_mask']),-1)
        assert masks[50,70]>0 and masks[92,122]>0
        with np.load(tmp_path/'analysis'/tile['contrast_rg_file']) as values:
            assert values['contrast_rg'].shape[-1]==2
