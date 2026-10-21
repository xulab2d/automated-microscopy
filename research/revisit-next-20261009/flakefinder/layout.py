"""Conservative chip-layout evidence from sparsely acquired raw camera frames.

The substrate reference must contain predominantly exposed substrate, under the
same camera/illumination settings. A colour mismatch is not a material identity.
No interpolation fills unobserved space and sparse positives never imply that
two observations belong to one chip. This module has no microscope interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import math
import cv2
import numpy as np

from .imaging import load_frame
from .model import CameraTransform, Frame, atomic_json

UNKNOWN, SUBSTRATE, NON_SUBSTRATE = 0, 1, 2
LABELS = {UNKNOWN: 'unknown', SUBSTRATE: 'substrate', NON_SUBSTRATE: 'non_substrate'}


def classify_seed_patch(labels, source_shape, matrix, diameter_um=40.):
    """Assess the physical patch at stage centre, independently of field edges.

    A valid seed may be near a chip corner, with much of the full image off-chip.
    This establishes local colour support only, never a chip outline.
    """
    if not np.isfinite(diameter_um) or diameter_um <= 0:
        raise ValueError('Seed patch diameter must be finite and positive.')
    mh, mw = labels.shape
    h, w = source_shape[:2]
    yy, xx = np.mgrid[:mh, :mw]
    pixels = np.stack([(xx+.5)*w/mw-w/2, (yy+.5)*h/mh-h/2], axis=-1)
    offsets = pixels @ np.asarray(matrix).T
    patch = np.sum(offsets**2, axis=2) <= (diameter_um/2)**2
    if patch.sum() < 9:
        raise ValueError('Detectable patch contains too few classification pixels.')
    substrate = float(np.mean(labels[patch] == SUBSTRATE))
    holder = float(np.mean(labels[patch] == NON_SUBSTRATE))
    return {'classification': 'substrate_seed' if substrate >= .9 else
            'non_substrate_sample' if holder >= .95 else 'uncertain',
            'patch_substrate_fraction': substrate, 'patch_non_substrate_fraction': holder}


def _context(frame):
    m = frame.metadata
    return {k: m.get(k) for k in ('camera_resources', 'camera_job_template',
            'objective_position', 'objective', 'white_level', 'illumination_percent')}


def _working_rgb(frame, max_dimension):
    h, w = frame.rgb.shape[:2]
    scale = min(1., max_dimension / max(h, w))
    a = cv2.resize(frame.rgb.astype(np.float32),
                   (max(2, round(w * scale)), max(2, round(h * scale))),
                   interpolation=cv2.INTER_AREA)
    return a


def _features(rgb, white_level):
    valid = (rgb.min(axis=2) > max(4., .02 * white_level)) & (rgb.max(axis=2) < .98 * white_level)
    ratios = np.log(np.maximum(rgb[:, :, [0, 2]], 1.) / np.maximum(rgb[:, :, 1:2], 1.))
    return ratios, valid


@dataclass
class SubstrateReference:
    centre: np.ndarray
    covariance: np.ndarray
    white_level: float
    context: dict
    pixel_size_um: tuple
    max_dimension: int = 768

    @classmethod
    def from_frames(cls, frames, max_dimension=768):
        """Pool known exposed-substrate references to retain field colour variation.

        Callers supply verified reference fields, not unclassified new probes.
        Equal weighting per field prevents one larger image dominating the fit.
        """
        frames = list(frames)
        if not frames:
            raise ValueError('At least one substrate reference is required.')
        model = cls.from_frame(frames[0], max_dimension)
        pools = []
        for frame in frames:
            if _context(frame) != model.context or not np.allclose(frame.pixel_size_um, model.pixel_size_um, rtol=.01):
                raise ValueError('Substrate references have different acquisition contexts.')
            values, valid = _features(_working_rgb(frame, max_dimension), model.white_level)
            pixels = values[valid]
            if len(pixels) < 100:
                raise ValueError('Reference has too few exposed pixels.')
            pixels = pixels[np.linspace(0, len(pixels)-1, min(20000, len(pixels))).astype(int)]
            centre = np.median(pixels, axis=0)
            spread = np.maximum(1.4826*np.median(abs(pixels-centre), axis=0), [.025, .012])
            pools.append(pixels[(abs((pixels-centre)/spread) < 3.).all(axis=1)])
        pixels = np.concatenate(pools)
        model.centre = np.median(pixels, axis=0)
        covariance = np.cov(pixels, rowvar=False)
        covariance += np.diag(np.maximum(0., np.array([.025, .012])**2-np.diag(covariance)))
        model.covariance = covariance+np.eye(2)*1e-10
        return model

    @classmethod
    def from_frame(cls, frame, max_dimension=768):
        if max_dimension < 32:
            raise ValueError('Use at least 32 pixels along the working image dimension.')
        white = frame.metadata.get('white_level')
        if white is None:
            if not np.issubdtype(frame.rgb.dtype, np.integer):
                raise ValueError('A raw floating-point image needs a white_level.')
            white = np.iinfo(frame.rgb.dtype).max
        if not math.isfinite(white) or white <= 0:
            raise ValueError('Invalid camera white level.')
        a, valid = _features(_working_rgb(frame, max_dimension), white)
        pixels = a[valid]
        if len(pixels) < 100:
            raise ValueError('Reference has too few exposed, unsaturated pixels.')
        centre = np.median(pixels, axis=0)
        spread = np.maximum(1.4826 * np.median(abs(pixels-centre), axis=0), [.025, .012])
        # Start with a robust component-wise gate so minority flakes do not
        # widen the substrate distribution to include a second colour mode.
        keep = (abs((pixels-centre)/spread) < 3.).all(axis=1)
        core = pixels[keep]
        if len(core) < 100 or len(core) < .5*len(pixels):
            raise ValueError('Reference is not predominantly one substrate colour.')
        centre = np.median(core, axis=0)
        covariance = np.cov(core, rowvar=False)
        covariance += np.diag(np.maximum(0., np.array([.025, .012])**2 - np.diag(covariance)))
        covariance += np.eye(2)*1e-10
        return cls(centre, covariance, float(white), _context(frame), tuple(frame.pixel_size_um), max_dimension)

    def classify(self, frame):
        if _context(frame) != self.context or not np.allclose(frame.pixel_size_um, self.pixel_size_um, rtol=.01):
            raise ValueError('Layout/reference acquisition context or pixel scale changed.')
        rgb = _working_rgb(frame, self.max_dimension)
        features, valid = _features(rgb, self.white_level)
        delta = features-self.centre
        distance = np.sqrt(np.maximum(0., np.einsum('...i,ij,...j->...', delta, np.linalg.inv(self.covariance), delta)))
        labels = np.zeros(valid.shape, np.uint8)
        labels[valid & (distance <= 3.5)] = SUBSTRATE
        labels[valid & (distance >= 6.)] = NON_SUBSTRATE
        return labels

    def to_dict(self):
        return {'features': ['log(R/G)', 'log(B/G)'], 'centre': self.centre.tolist(),
                'covariance': self.covariance.tolist(), 'white_level': self.white_level,
                'context': self.context, 'pixel_size_um': list(self.pixel_size_um),
                'working_max_dimension': self.max_dimension,
                'substrate_mahalanobis_max': 3.5, 'mismatch_mahalanobis_min': 6.,
                'interpretation': 'Reference colour match; neither chemical identity nor proof of chip continuity.'}


@dataclass
class Observation:
    name: str
    frame: Frame
    transform: CameraTransform
    labels: np.ndarray
    source: str | None = None

    def __post_init__(self):
        self.transform.validate_frame(self.frame)
        if self.labels.ndim != 2 or not np.isin(self.labels, [0, 1, 2]).all():
            raise ValueError('Invalid substrate labels.')

    def source_pixel(self, working_pixel):
        h, w = self.frame.rgb.shape[:2]
        mh, mw = self.labels.shape
        return (np.asarray(working_pixel, float)+.5)*[w/mw, h/mh]-.5

    def stage_xy(self, working_pixel):
        return self.transform.stage_xy(self.source_pixel(working_pixel), self.frame)

    def query(self, xy):
        h, w = self.frame.rgb.shape[:2]
        pixel = np.linalg.solve(np.asarray(self.transform.matrix), np.asarray(xy)-[self.frame.position.x, self.frame.position.y])
        pixel += [(w-1)/2, (h-1)/2]
        if not (-.5 <= pixel[0] < w-.5 and -.5 <= pixel[1] < h-.5):
            return None  # Distinct from measured-but-ambiguous UNKNOWN.
        mh, mw = self.labels.shape
        ix, iy = np.floor((pixel+.5)*[mw/w, mh/h]).astype(int)
        return int(self.labels[min(iy, mh-1), min(ix, mw-1)])

    def footprint(self):
        h, w = self.frame.rgb.shape[:2]
        return [self.transform.stage_xy(p, self.frame).tolist() for p in
                [(-.5, -.5), (w-.5, -.5), (w-.5, h-.5), (-.5, h-.5)]]

    def summary(self):
        fractions = {LABELS[v]: float(np.mean(self.labels == v)) for v in (0, 1, 2)}
        if fractions['substrate'] >= .85:
            kind = 'substrate'
        elif fractions['non_substrate'] >= .85:
            kind = 'non_substrate'
        elif min(fractions['substrate'], fractions['non_substrate']) >= .08:
            kind = 'mixed'
        else:
            kind = 'uncertain'
        return {'name': self.name, 'source': self.source, 'position_um': [self.frame.position.x, self.frame.position.y, self.frame.position.z],
                'classification': kind, 'fractions': fractions, 'centre_classification': LABELS[self.query([self.frame.position.x, self.frame.position.y])],
                'footprint_um': self.footprint(), 'label_shape': list(self.labels.shape),
                'boundary_segments': boundary_segments(self)}


def boundary_segments(observation, min_length_um=35.):
    """Internal colour boundaries supported on both sides, excluding the FOV rim.

    A line is evidence of an interface, not an automatically closed chip polygon.
    Small enclosed flake/residue holes cannot pass the macro-region area gate.
    """
    labels = observation.labels
    h, w = labels.shape
    kernel = np.ones((5, 5), np.uint8)
    substrate = cv2.morphologyEx((labels == SUBSTRATE).astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    substrate = cv2.morphologyEx(substrate, cv2.MORPH_OPEN, kernel)
    count, components, stats, _ = cv2.connectedComponentsWithStats(substrate)
    large = np.zeros_like(substrate)
    for i in range(1, count):
        if stats[i, cv2.CC_STAT_AREA] >= .08*h*w:
            large[components == i] = 1
    if not large.any() or np.mean(labels == NON_SUBSTRATE) < .08:
        return []
    contours, _ = cv2.findContours(large, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result = []
    for contour in contours:
        approximation_px = max(2., .0125*min(h, w))
        polygon = cv2.approxPolyDP(contour, approximation_px, True).reshape(-1, 2)
        for a, b in zip(polygon, np.roll(polygon, -1, axis=0)):
            # A clipped outline coinciding with the image edge is no evidence.
            midpoint = (a+b)/2
            if min(midpoint[0], midpoint[1], w-1-midpoint[0], h-1-midpoint[1]) < 4:
                continue
            vector = b-a
            norm = np.linalg.norm(vector)
            if norm == 0:
                continue
            ends = [observation.stage_xy(p) for p in (a, b)]
            if np.linalg.norm(ends[1]-ends[0]) < min_length_um:
                continue
            normal = np.array([-vector[1], vector[0]])/norm
            supported = 0
            support_distances = []
            for t in (.2, .35, .5, .65, .8):
                centre = a+t*vector
                # A bevel or defocused edge can have an uncertain band between
                # exposed substrate and the holder. Look on both sides, retain
                # the actual reach as uncertainty, and never label that band.
                for distance in (2, 4, 8, 16, 32):
                    pair = []
                    for direction in (-1, 1):
                        p = np.rint(centre+direction*normal*distance).astype(int)
                        pair.append(int(labels[p[1], p[0]]) if 0 <= p[0] < w and 0 <= p[1] < h else UNKNOWN)
                    if set(pair) == {SUBSTRATE, NON_SUBSTRATE}:
                        supported += 1
                        support_distances.append(distance)
                        break
            if supported < 3:
                continue
            result.append({'endpoints_px': [observation.source_pixel(p).tolist() for p in (a, b)],
                           'endpoints_um': [p.tolist() for p in ends], 'support_fraction': supported/5,
                           'uncertainty_um': float(np.linalg.norm(observation.stage_xy(midpoint+max(max(support_distances), approximation_px)*normal)-observation.stage_xy(midpoint))),
                           'kind': 'observed_substrate_colour_interface'})
    return result


@dataclass
class SparseOccupancy:
    observations: list[Observation] = field(default_factory=list)

    def query(self, xy):
        measured = [v for o in self.observations if (v := o.query(xy)) is not None]
        if not measured:
            return {'status': 'unobserved', 'observations': 0}
        confident = set(measured)-{UNKNOWN}
        label = next(iter(confident)) if len(confident) == 1 else UNKNOWN
        return {'status': LABELS[label], 'observations': len(measured), 'conflict': len(confident) > 1}

    def _observed_reach(self, start, other):
        p = np.array([start.frame.position.x, start.frame.position.y])
        q = np.array([other.frame.position.x, other.frame.position.y])
        label = start.query(p)
        if label not in (SUBSTRATE, NON_SUBSTRATE):
            return p
        # Sampling is finer than one working-mask pixel and cannot jump holes.
        step = min(start.frame.pixel_size_um)*min(start.frame.rgb.shape[:2])/max(start.labels.shape)/2
        n = max(2, int(np.ceil(np.linalg.norm(q-p)/step)))
        last = p
        for t in np.linspace(0, 1, n+1)[1:]:
            xy = p+t*(q-p)
            if start.query(xy) != label:
                break
            last = xy
        return last

    def report(self, alignment_tolerance_um=2., minimum_gap_um=20.):
        records = [o.summary() for o in self.observations]
        brackets, recommendations, gaps = [], [], []
        positions = np.array([[o.frame.position.x, o.frame.position.y] for o in self.observations])
        seen = set()
        for i, p in enumerate(positions):
            for axis in (0, 1):
                candidates = [j for j, q in enumerate(positions) if q[axis] > p[axis]+alignment_tolerance_um
                              and abs(q[1-axis]-p[1-axis]) <= alignment_tolerance_um]
                if not candidates:
                    continue
                j = min(candidates, key=lambda j: positions[j, axis])
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                a, b = self.observations[i], self.observations[j]
                la, lb = a.query(p), b.query(positions[j])
                left, right = self._observed_reach(a, b), self._observed_reach(b, a)
                gap = float(right[axis]-left[axis])
                if gap <= minimum_gap_um:
                    continue
                midpoint = (left+right)/2
                # A third measured frame can resolve this proposed location.
                if self.query(midpoint)['status'] != 'unobserved':
                    continue
                names = [a.name, b.name]
                common = {'axis': 'xy'[axis], 'probe_names': names,
                          'endpoints_um': [left.tolist(), right.tolist()], 'width_um': gap,
                          'next_xy_um': midpoint.tolist()}
                if {la, lb} == {SUBSTRATE, NON_SUBSTRATE}:
                    brackets.append({**common, 'transition_count': 'one or more',
                                     'interpretation': 'Colour transition bracket, not a uniquely identified outer chip edge.'})
                    priority, reason = 1, 'bisect_substrate_transition'
                else:
                    gaps.append({**common, 'endpoint_classes': [LABELS[la], LABELS[lb]],
                                 'status': 'unobserved; continuity unknown'})
                    priority, reason = (2, 'test_continuity_between_substrate_observations') if la == lb == SUBSTRATE else (3, 'sample_unknown_gap')
                recommendations.append({**common, 'priority': priority, 'reason': reason})
        recommendations.sort(key=lambda r: (r['priority'], -r['width_um'], r['axis'], r['next_xy_um']))
        return {'schema': 1, 'kind': 'sparse_substrate_layout', 'observations': records,
                'edge_brackets': brackets, 'unknown_gaps': gaps, 'recommendations': recommendations,
                'grid': {'status': 'unresolved', 'chip_count': None,
                         'reason': 'Sparse colour matches do not establish connected chips or grid slots.'},
                'coverage_rule': 'Only observed footprints contain evidence. All gaps are unknown, including between matching substrate probes.',
                'next_step': 'Acquire one recommended midpoint, append it to the observations, then recompute; do not assume a unique boundary inside a bracket.'}


def analyze_layout_probes(folder, reference_frame, output=None):
    """Analyze saved sparse probes; return (serializable report, SparseOccupancy).

    No motion is performed. ``reference_frame`` may be a Frame or saved TIFF.
    Optional output stores a report and compressed per-pixel evidence labels.
    """
    folder = Path(folder)
    manifest = json.loads((folder/'probes.json').read_text(encoding='utf-8'))
    config = manifest['configuration']['camera_transform']
    transform = CameraTransform(**config)
    if not transform.calibrated:
        raise ValueError('Physical boundary inference requires a measured camera transform.')
    reference = reference_frame if isinstance(reference_frame, Frame) else load_frame(reference_frame)
    model = SubstrateReference.from_frame(reference)
    occupancy = SparseOccupancy()
    for item in manifest['records']:
        name = item['name']
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('Invalid probe filename.')
        path = folder/'frames'/(name+'.tiff')
        frame = load_frame(path)
        occupancy.observations.append(Observation(name, frame, transform, model.classify(frame), str(path.resolve())))
    report = occupancy.report()
    report['reference'] = model.to_dict()
    report['probe_manifest_status'] = manifest.get('status')
    report['nominal_sample_pitch_um'] = manifest.get('sample_pitch_um')
    if output is not None:
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        for observation in occupancy.observations:
            np.savez_compressed(output/(observation.name+'-labels.npz'), labels=observation.labels)
        atomic_json(output/'layout.json', report)
    return report, occupancy
