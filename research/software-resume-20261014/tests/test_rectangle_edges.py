"""Deterministic raw-image and geometry checks; no adapters or hardware."""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from flakefinder.rectangle_edges import (
    rectangle_orientation, measure_rectangle_side, fit_rectangle_outline,
)


def raw_frame(matrix, normal=(1.,0.), debris=False, uniform=False):
    h,w = 256,320
    matrix = np.asarray(matrix)
    yy,xx = np.indices((h,w))
    pixel = np.stack([xx-w/2,yy-h/2],axis=-1)
    stage = pixel@matrix.T
    signed = stage@np.asarray(normal)
    fraction = (1-np.tanh(signed/1.5))/2
    rgb = 120 + fraction[:,:,None]*np.array([800.,1200.,1700.])
    if uniform:
        rgb[:] = 800.
    if debris:
        # Obscure substantial edge segments, plus unrelated high-contrast spots.
        rgb[40:90,:,:] = [180,190,200]
        rgb[170:210,100:235,:] = [2600,2600,2600]
        rgb[15:30,25:80,:] = [40,40,40]
    return SimpleNamespace(rgb=rgb.astype(np.uint16),position=SimpleNamespace(x=1234.,y=-4567.,z=20.))


@pytest.mark.parametrize('matrix', [
    [[.5,0],[0,.5]],
    [[-.5,0],[0,.5]],
    [[.4,-.3],[.3,.4]],
    [[-.4,-.3],[-.3,.4]],
])
def test_local_side_survives_debris_rotation_and_mirror(matrix):
    theta = math.radians(18)
    normal = [math.cos(theta),math.sin(theta)]
    frame = raw_frame(matrix,normal,debris=True)
    centre = np.dot(normal,[frame.position.x,frame.position.y])
    edge, report = measure_rectangle_side(frame,matrix,normal,offset_bracket_um=[centre-15,centre+15])
    assert edge is not None, report
    assert report['accepted'] and report['focus_needed'] is False
    assert report['supported_profiles'] >= 8
    assert abs(np.dot(edge['normal'],[frame.position.x,frame.position.y])-edge['offset_um']) < 4
    assert np.dot(edge['normal'],normal) > math.cos(math.radians(4))
    assert len(edge['points_um']) == report['supported_profiles']


def test_uniform_image_and_unrelated_bracket_do_not_invent_side():
    matrix = [[.5,0],[0,.5]]
    edge, report = measure_rectangle_side(raw_frame(matrix,uniform=True),matrix,[1,0])
    assert edge is None and not report['focus_needed']
    edge, report = measure_rectangle_side(raw_frame(matrix),matrix,[1,0],offset_bracket_um=[1254,1264])
    assert edge is None


def measured_edges(angle=27., perturb=False):
    theta = math.radians(angle)
    edges = []
    for i in range(4):
        n = np.array([math.cos(theta+i*math.pi/2),math.sin(theta+i*math.pi/2)])
        t = np.array([-n[1],n[0]])
        distance = 5000 if i%2==0 else 4000
        points = distance*n + np.linspace(-500,500,40)[:,None]*t
        normal = n.copy()
        if perturb:
            delta = math.radians([1.,-1.2,.5,-.3][i])
            rotation = np.array([[math.cos(delta),-math.sin(delta)],[math.sin(delta),math.cos(delta)]])
            normal = rotation@n
            points = distance*n + np.linspace(-500,500,40)[:,None]*(rotation@t)
        edges.append({'normal':normal.tolist(),'offset_um':float(normal@(distance*n)),
                      'points_um':points.tolist(),'image':'side%d.tiff'%i})
    return edges


def test_rectangle_requires_four_measured_locations_not_orientation_only():
    edges = measured_edges()
    assert rectangle_orientation(edges[:3])['status'] == 'estimated'
    result = fit_rectangle_outline(edges[:3],[0,0],[-15000,15000,-15000,15000])
    assert result['status'] == 'unresolved'
    assert result['polygon_um'] == []
    # Merely supplying fourth-side orientation/offset is insufficient.
    result = fit_rectangle_outline(edges[:3]+[{'normal':edges[3]['normal'],'offset_um':4000}],
                                   [0,0],[-15000,15000,-15000,15000])
    assert result['status'] == 'unresolved'


def test_shared_rotated_rectangle_and_nonorthogonality_uncertainty():
    clean = fit_rectangle_outline(measured_edges(),[0,0],[-15000,15000,-15000,15000])
    result = fit_rectangle_outline(measured_edges(perturb=True),[0,0],[-15000,15000,-15000,15000])
    assert clean['status'] == result['status'] == 'estimated'
    assert len(result['polygon_um']) == len(result['lines']) == 4
    assert result['uncertainty_um'] > clean['uncertainty_um']
    p = np.asarray(result['polygon_um']); sides = np.roll(p,-1,axis=0)-p
    assert abs(float(sides[0]@sides[1])) < 1e-6
    assert result['all_chips_found'] is False


def test_rectangle_cannot_expand_beyond_explicit_envelope():
    result = fit_rectangle_outline(measured_edges(),[0,0],[-1000,1000,-1000,1000])
    assert result['status'] == 'unresolved'
    assert result['polygon_um'] == []


def test_invalid_transform_rejected():
    matrix = [[.5,0],[0,.5]]
    with pytest.raises(ValueError):
        measure_rectangle_side(raw_frame(matrix),[[1,0],[0,0]],[1,0])
