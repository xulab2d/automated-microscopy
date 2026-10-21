"""Inspectability and physical screening for the separate empirical proposal mode."""
from dataclasses import asdict
import copy
import json

import numpy as np
from PIL import Image

from flakefinder.analysis import analyze_scan,empirical_proposals,extract_regions
from flakefinder.colors import AnalysisOptions,ColorGroups,prepare_contrast
from flakefinder.imaging import save_frame
from flakefinder.model import CameraTransform,Frame,Position,atomic_json


def diffuse_scene():
    yy,xx=np.mgrid[:250,:320]
    hump=.12*np.exp(-((xx-170)**2+(yy-125)**2)/(2*28**2))
    image=np.full((250,320,3),2000.,float)*(1-hump[:,:,None])
    return Frame(np.uint16(image),Position(1000,2000,30),(.5,.5),0)


def test_empirical_retains_optical_region_and_strict_rejection_evidence():
    frame=diffuse_scene();options=AnalysisOptions(max_components=2)
    prepared=prepare_contrast(frame,options)
    groups=ColorGroups.fit(prepared['smooth'][prepared['foreground']],2)
    strict,_=extract_regions(frame,prepared,groups,options)
    assert strict and all(r['disposition']=='rejected' for r in strict)
    proposals=empirical_proposals(copy.deepcopy(strict),options)
    accepted=[r for r in proposals if r['disposition']=='candidate']
    assert accepted and any('unsupported_or_diffuse_boundary' in r['evidence_warnings'] for r in accepted)
    assert all(r['strict_disposition']=='rejected' and r['layer_number'] is None for r in accepted)
    # Reconsideration does not alter boundaries or silently relax physical width.
    assert proposals[0]['polygon_px']==strict[0]['polygon_px']
    narrow=copy.deepcopy(strict[0]);narrow['usable_width_um']=2
    for terrace in narrow['terraces']:terrace['usable_width_um']=2
    rejected=empirical_proposals([narrow],options)[0]
    assert rejected['disposition']=='rejected' and 'insufficient_usable_width' in rejected['filter_reasons']


def test_empirical_review_has_separate_ranked_raw_and_outline_sheets(tmp_path):
    frame=diffuse_scene();name='tile_000000';save_frame(tmp_path/'tiles',name,frame)
    transform=CameraTransform([[.5,0],[0,.5]],(.5,.5),True)
    atomic_json(tmp_path/'manifest.json',{'transform':asdict(transform),'plan':{'chip_id':'test','polygon_um':None},
        'completed':{name:{'position':asdict(frame.position)}},'context':{'material':'MoTe2','oxide_nm':285}})
    before=(tmp_path/'tiles'/f'{name}.tiff').read_bytes()
    summary=analyze_scan(tmp_path,{'backend':'empirical','max_components':2})
    assert summary['backend']=='empirical_optical_proposals' and summary['candidate_count']>=1
    index=json.loads((tmp_path/'analysis/contact-sheets/index.json').read_text())
    sheet=index['sheets'][0]
    raw=np.array(Image.open(tmp_path/'analysis'/sheet['raw']))
    outlines=np.array(Image.open(tmp_path/'analysis'/sheet['outlines']))
    assert raw.shape==outlines.shape and np.any(raw!=outlines)
    assert index['index'][0]['source_image']=='tiles/tile_000000.tiff'
    assert (tmp_path/'tiles'/f'{name}.tiff').read_bytes()==before
    rows=json.loads((tmp_path/'analysis/analysis.json').read_text())
    assert rows['contact_sheets']['displayed_count']==summary['candidate_count']
