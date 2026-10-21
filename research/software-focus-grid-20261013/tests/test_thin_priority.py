from flakefinder.analysis import assign_mote2_screen


def test_quality_failed_color_match_is_not_a_thin_candidate():
    row={'contrast_rgb':[.06,.07,.04],'area_um2':400,'disposition':'candidate',
         'strict_disposition':'rejected','parent_filter_reasons':['nonuniform_terraces','textured_interior']}
    assign_mote2_screen(row)
    assert row['priority']=='color_match_only' and row['thin_screen']['score']==0
    assert row['color_compatibility']['score']==1
    assert row['thin_screen_area_um2']==0 and not row['thin_screen']['object_evidence_passed']


def test_strictly_supported_terrace_can_qualify_inside_rejected_parent():
    row={'contrast_rgb':[.06,.07,.04],'area_um2':400,'disposition':'candidate',
         'strict_disposition':'candidate','parent_filter_reasons':['textured_interior'],
         'terraces':[{'contrast_rgb':[.1,.12,.06],'area_um2':120,'disposition':'candidate',
                      'strict_disposition':'candidate','evidence_warnings':[]}]}
    assign_mote2_screen(row)
    assert row['priority']=='thin_screen' and row['thin_screen_region']=='terrace:0'
    assert row['thin_screen_area_um2']==120
    assert row['color_compatibility_region']=='parent'
