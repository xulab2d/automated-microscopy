"""Reproduce the manufacturer's plotted curves from vector Bezier paths.

Requires the bundled PDF runtime; never accesses camera/hardware.
"""
from pathlib import Path
import hashlib,json
import numpy as np
import pdfplumber

ROOT=Path(__file__).resolve().parent
pdf=ROOT/'Axiocam305_vendor_flyer.pdf'
with pdfplumber.open(pdf) as document:
    page=document.pages[1]
    curves=[c for c in page.curves if c['width']>200 and len(c['pts'])==33]
    assert len(curves)==3
    # Axis positions independently checked against visible numeric ticks/gridlines.
    x400,x720=73.6873,333.3033
    yzero,yone=702.8742,554.3586
    wave=np.arange(400.,721.)
    channels={}
    for curve in curves:
        points=[];last=None
        for segment in curve['path']:
            if segment[0]=='m':last=np.array(segment[1],float);points.append(last)
            elif segment[0]=='c':
                controls=np.array([last,*segment[1:]],float)
                t=np.linspace(0,1,21)[:,None]
                q=(1-t)**3*controls[0]+3*(1-t)**2*t*controls[1]+3*(1-t)*t*t*controls[2]+t**3*controls[3]
                points.extend(q[1:]);last=controls[-1]
            else:raise ValueError('unexpected PDF path operation')
        points=np.array(points)
        wavelength=400+(points[:,0]-x400)*320/(x720-x400)
        response=(yzero-points[:,1])/(yzero-yone)
        channel=int(np.argmax(curve['stroking_color']))
        # Curve colors unambiguously map0=red,1=green,2=blue.
        channels[channel]=np.interp(wave,wavelength,response)
    rgb=np.column_stack([channels[i] for i in range(3)])
    payload={'schema':1,'kind':'manufacturer_typical_rgb_sensitivity_vector_trace',
      'source':'https://asset-downloads.zeiss.com/catalogs/download/mic/29e65723-e381-4197-aece-157ee2d598e2/EN_product-flyer_Axiocam_305_color.pdf',
      'source_pdf_sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),'page_1based':2,
      'manufacturer_document':'ZEISS EN_40_012_109 CZ07-2017','sensor':'Axiocam305color incl.IRfilterHoya5000',
      'wavelengths_nm':wave.tolist(),'relative_sensitivity_rgb':rgb.tolist(),
      'x_axis_pdf_points':[x400,x720],'y_axis_pdf_points':[yzero,yone],
      'extraction':'cubicBezier centerlines sampled at20subintervals per10nm segment; linear interpolation to1nm',
      'plot_half_stroke_uncertainty_response':1.081/2/(yzero-yone),
      'plot_half_stroke_uncertainty_nm':1.081/2/(x720-x400)*320,
      'uncertainty_warning':'Drawing scale only, NOT a confidence bound on sensor sensitivity. Manufacturer gives no device-to-device uncertainty or incident-photon-versus-radiant-power convention.',
      'actual_camera_response_measured':False,'lamp_spectrum_included':False,'acquisition_color_matrix_included':False}
    out=ROOT.parents[1]/'configs/materials/data/Axiocam305_vendor_response.json'
    out.write_text(json.dumps(payload,indent=2),encoding='utf-8')
    print(out, 'RGB peaks',rgb.max(axis=0),'halfstroke',payload['plot_half_stroke_uncertainty_response'])
