"""Known-mask segmentation, provenance and motion-tolerance regression tests."""
from dataclasses import asdict
import json
import cv2
import numpy as np
import pytest
from flakefinder.model import Frame,Position,CameraTransform,AcquisitionError,atomic_json
from flakefinder.colors import AnalysisOptions,ColorGroups,prepare_contrast
from flakefinder.analysis import extract_regions,analyze_scan
from flakefinder.imaging import save_frame
from flakefinder.chips import detect_chips
from flakefinder.zen import PositionToleranceError
from test_integration import bare_zen

def specimen(seed=2):
    rng=np.random.default_rng(seed);h,w=300,420
    yy,xx=np.mgrid[:h,:w];flat=1-.1*((xx-w/2)/w)**2-.1*((yy-h/2)/h)**2
    image=np.broadcast_to([1800.,1600.,2200.],(h,w,3)).copy()*flat[:,:,None]
    masks=[]
    for i in range(6):
        x=45+(i%3)*115;y=40+(i//3)*120
        c=np.array([.035,.06,.02]) if i%2 else np.array([.085,.13,.04])
        mask=np.zeros((h,w),np.uint8);cv2.rectangle(mask,(x,y),(x+30,y+36),1,-1)
        image[mask>0]*=1-c;masks.append(mask)
    # Narrow, connected scratch: measurable contrast but poor useful flake shape.
    cv2.line(image,(20,270),(395,270),(700,700,700),3)
    image+=rng.normal(0,2,image.shape)
    return Frame(np.uint16(image),Position(1000,2000,30),(1,1),1),masks

def test_color_segmentation_recovers_known_regions_and_rejects_scratch():
    frame,truth=specimen();options=AnalysisOptions(max_components=3)
    prep=prepare_contrast(frame,options);values=prep['smooth'][prep['foreground']]
    groups=ColorGroups.fit(values,3)
    rows,labels=extract_regions(frame,prep,groups,options,CameraTransform([[1,0],[0,1]],(1,1),True))
    candidates=[r for r in rows if r['disposition']=='candidate']
    assert len(candidates)==6
    for target in truth:
        matches=[]
        for row in candidates:
            got=np.zeros(target.shape,np.uint8);cv2.fillPoly(got,[np.int32(row['polygon_px'])],1)
            matches.append(np.sum((got>0)&(target>0))/np.sum((got>0)|(target>0)))
        assert max(matches)>.85
    assert any('ragged_or_filamentary' in r['filter_reasons'] for r in rows)
    assert len({r['color_group'] for r in candidates})==2
    assert all(r['label']=='unclassified' for r in rows)

def test_blank_noise_field_has_no_foreground():
    rng=np.random.default_rng(10)
    frame=Frame(np.uint16(2000+rng.normal(0,2,(240,320,3))),Position(0,0,0),(1,1),0)
    prep=prepare_contrast(frame,AnalysisOptions())
    assert prep['foreground'].sum()==0

def test_unknown_chip_count_and_independently_rotated_grid():
    rgb=np.full((600,800,3),20,np.uint8)
    for y in (120,350):
        for x in (120,350,580):
            poly=np.int32(cv2.boxPoints(((x,y),(90,100),22)))
            cv2.fillPoly(rgb,[poly],(180,170,160))
    chips,_=detect_chips(rgb,min_area_px=1000)
    assert len(chips)==6 and chips[0].id=='chip-001'
    grid,_=detect_chips(rgb,min_area_px=1000,grid_shape=(2,3))
    assert {c.id for c in grid}=={'A1','A2','A3','B1','B2','B3'}
    single,_=detect_chips(rgb[:235,:235],min_area_px=1000,grid_shape=(1,1))
    assert single[0].id=='A1'

def test_analysis_keeps_two_tile_identities_and_crop_coordinates(tmp_path):
    transform=CameraTransform([[1,0],[0,1]],(1,1),True)
    completed={}
    for i in range(2):
        frame,_=specimen(i);frame.position=Position(1000+i*500,2000,30)
        name=f'tile_{i:06d}';save_frame(tmp_path/'tiles',name,frame)
        completed[name]={'position':asdict(frame.position)}
    manifest={'transform':asdict(transform),'plan':{'chip_id':None,'polygon_um':None},'completed':completed}
    atomic_json(tmp_path/'manifest.json',manifest)
    raw_before=(tmp_path/'tiles/tile_000000.tiff').read_bytes()
    summary=analyze_scan(tmp_path,{'max_components':3,'backend':'classical'})
    assert summary['distinct_images']==2 and summary['candidate_count']==12
    review=json.loads((tmp_path/'analysis/review-data.json').read_text())
    assert len({r['tile_image'] for r in review['flakes']})==2
    for row in review['flakes']:
        x0,y0,x1,y1=row['crop_bounds_px'];x,y=row['centroid_px']
        assert x0<=x<x1 and y0<=y<y1
        assert (tmp_path/'analysis'/row['crop_image']).exists()
    assert (tmp_path/'tiles/tile_000000.tiff').read_bytes()==raw_before

def test_vendor_tolerance_warning_requires_stationary_correct_readback():
    s=bare_zen();state=[Position(10000,20000,3000)];s.position=lambda:state[0]
    def near_target(*_):
        state[0]=Position(10000,20000,3002.078)
        raise PositionToleranceError('Requested position was not reached.')
    s._motion_rpc=near_target
    assert s.move_z(3002).z==pytest.approx(3002.078)
    assert s.armed
    state[0]=Position(10000,20000,3000);stops=[];s._rpc=lambda *a,**k:stops.append(a)
    def wrong_target(*_):raise PositionToleranceError('Requested position was not reached.')
    s._motion_rpc=wrong_target
    with pytest.raises(PositionToleranceError):s.move_z(3002)
    assert not s.armed and len(stops)==2

def test_axis_travel_does_not_subdivide_small_axis_correction():
    s=bare_zen();state=[Position(10000,20000,3000)];moves=[];s.position=lambda:state[0]
    def move(p):s.limits.check_move(state[0],p);moves.append(p);state[0]=p
    s.move=move;s._travel_direct(10600,19970)
    assert all(p.y==20000 for p in moves[:-1])
    assert moves[-1]==Position(10600,19970,3000)
