import cv2
import numpy as np

from flakefinder.analysis import _refine_objects,extract_regions
from flakefinder.colors import AnalysisOptions,ColorGroups,prepare_contrast
from flakefinder.model import Frame,Position


def test_one_foreground_envelope_retains_disconnected_different_color_objects():
    image=np.full((180,240,3),2000.,float)
    image[65:100,65:100]*=[1.6,1.6,1.6]
    image[65:100,125:145]*=[.7,1.6,1.35]
    frame=Frame(np.uint16(image),Position(0,0,0),(.25,.25),0)
    options=AnalysisOptions()
    prepared=prepare_contrast(frame,options)
    prepared['foreground'][:]=False
    prepared['foreground'][55:110,55:155]=True
    labels=_refine_objects(frame,prepared,options,return_labels=True)
    assert labels[80,80]>0 and labels[80,135]>0
    assert labels[80,80]!=labels[80,135] and labels[80,115]==0
    rows,mask=extract_regions(frame,prepared,ColorGroups({'groups':[]}),options)
    assert len(rows)==2 and all(row['area_um2']<100 for row in rows)
    assert all('below_useful_area' in row['filter_reasons'] for row in rows)
    assert mask[80,80]>0 and mask[80,135]>0 and mask[80,80]!=mask[80,135]
