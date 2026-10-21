from flakefinder.grid_layout import build_verified_layout


def grid():
    return {'axes':{},'cells':[{'index':[0,0],'xy_um':[10000.,10000.]},{'index':[1,0],'xy_um':[22000.,10000.]}]}


def observed(x,y,label='substrate',fraction=.99,name='probe'):
    return {'name':name,'position_um':[x,y,0.],'centre_classification':label,
            'fractions':{'substrate':fraction},'source':f'C:/data/{name}.tiff'}


def test_negative_centre_is_unresolved_even_after_negative_offset_search():
    r=build_verified_layout(grid(),[observed(10000,10000,'non_substrate',0.),observed(8000,10000,'non_substrate',0.)])
    assert not r['chips']
    assert len(r['missing_slots'])==2
    assert r['confirmed_missing_slots']==[]


def test_shifted_survey_centre_requires_whole_field_substrate_support():
    observations=[observed(10000,10000,'non_substrate',0.,'holder'),
                  observed(8000,10000,'substrate',.4,'mixed-edge'),
                  observed(12000,10000,'substrate',.99,'shifted'),
                  observed(22000,10000,'substrate',.99,'centre')]
    r=build_verified_layout(grid(),observations)
    assert len(r['chips'])==2
    shifted=r['chips'][0]
    assert shifted['id']=='R1C1'
    assert shifted['centre_um']==[12000,10000]
    assert shifted['nominal_centre_um']==[10000,10000]
    assert shifted['centre_kind']=='shifted_observed_interior'
    assert shifted['source_image']=='C:/data/shifted.tiff'
    assert not r['missing_slots']


def test_off_grid_or_unobserved_positions_cannot_fill_a_slot():
    r=build_verified_layout(grid(),[observed(50000,50000),observed(10000,10000,'unknown',.99)])
    assert not r['chips']
