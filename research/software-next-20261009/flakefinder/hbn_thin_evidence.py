"""Conditional thin-film branch evidence, independent of feature geometry.

Profile explicit nuisance scenarios individually. Distances and likelihood ratios
are engineering comparisons, not material probabilities or thickness calibration.
Caller must measure the intended object, not reuse an adjacent bulk-edge mask.
"""
from dataclasses import dataclass
import numpy as np
from scipy.stats import chi2

from .material_contrast import covariance
from .material_profiles import CONVENTION, canonical_hash


POLICY_VERSION = 'hbn-thin-film-evidence-v2'
MATCH_RADIUS2 = float(chi2.ppf(.9973, 3))
MINIMUM_LOG_RATIO = float(np.log(20.))


@dataclass(frozen=True)
class CompiledOpticalModel:
    thickness_nm: np.ndarray
    contrast_rgb: np.ndarray
    digest: str
    model_id: str
    material: str
    contrast_convention: str

    def get(self, name, default=None):
        return getattr(self, name, default)

    def __getitem__(self, name):
        return getattr(self, name)


def compile_optical_model(model):
    """Snapshot validated arrays and a source hash once, never once per region."""
    if isinstance(model, CompiledOpticalModel):
        return model
    _profile(np.zeros(3), np.eye(3), model)
    thickness=np.array(model['thickness_nm'],float,copy=True)
    curves=np.array(model['contrast_rgb'],float,copy=True)
    if curves.ndim==2: curves=curves[None,...]
    thickness.flags.writeable=False;curves.flags.writeable=False
    return CompiledOpticalModel(thickness,curves,canonical_hash(model),model.get('model_id'),
                                model.get('material'),model.get('contrast_convention'))


def _model_hash(model):
    return model.digest if isinstance(model,CompiledOpticalModel) else canonical_hash(model)


def _profile(rgb, inverse, model):
    if isinstance(model,CompiledOpticalModel):
        delta=model.contrast_rgb-rgb
        return model.thickness_nm,np.einsum('...i,ij,...j->...',delta,inverse,delta)
    if model.get('contrast_convention') != CONVENTION:
        raise ValueError('Explicit substrate-minus-feature contrast convention required.')
    t = np.asarray(model['thickness_nm'], float)
    curves = np.asarray(model['contrast_rgb'], float)
    if curves.ndim == 2:
        curves = curves[None, ...]
    if (t.ndim != 1 or not len(t) or not np.isfinite(t).all()
            or np.any(t < 0) or np.any(np.diff(t) <= 0)
            or curves.ndim != 3 or curves.shape[1:] != (len(t), 3)
            or not len(curves) or not np.isfinite(curves).all()):
        raise ValueError('Finite scenario/thickness/RGB tables with increasing thickness required.')
    delta = curves - rgb
    distances = np.einsum('...i,ij,...j->...', delta, inverse, delta)
    return t, distances


def _best(t, distances, mask):
    if not np.any(mask):
        return None
    selected = distances[:, mask]
    scenario, index = np.unravel_index(np.argmin(selected), selected.shape)
    return {'distance2': float(selected[scenario, index]),
            'distance': float(np.sqrt(selected[scenario, index])),
            'conditional_best_nm': float(t[mask][index]),
            'scenario_index': int(scenario)}


def score_thin_evidence(rgb, cov, hbn_model, *, alternative_models=(),
                        model_covariance=None, operator_annotation=None):
    """Return review evidence for <5 nm and 5..30 nm versus >30..500 nm.

    ``operator_annotation`` may be {'label':'thin_positive'|'bulk',
    'source':<nonempty provenance>}. It labels the actual measured feature only;
    it supplies neither a thickness nor an optical calibration. A thin-positive
    can bypass alternative-film ambiguity, never a failed optical/noise check or
    a bulk alias. Missing alternative models remain explicitly unassessed.
    """
    rgb = np.asarray(rgb, float)
    if rgb.shape != (3,) or not np.isfinite(rgb).all():
        raise ValueError('One finite measured RGB contrast required.')
    noise = covariance(cov, 'measurement covariance')
    extra = covariance(np.eye(3) * .0001 if model_covariance is None else model_covariance,
                       'model covariance', False)
    total = noise + extra
    inverse = np.linalg.inv(total)
    if hbn_model.get('material') != 'hBN':
        raise ValueError('Explicit hBN model required.')
    t, distances = _profile(rgb, inverse, hbn_model)
    target = (t > 0) & (t <= 30)
    bulk = (t > 30) & (t <= 500)
    if not np.any(target & (t < 5)) or not np.any(target & (t >= 5)) or not np.any(bulk):
        raise ValueError('Model must cover both target bands and bulk aliases through its supplied grid.')
    thin_best = _best(t, distances, target)
    bulk_best = _best(t, distances, bulk)
    best = _best(t, distances, target | bulk)
    null_d2 = float(rgb @ np.linalg.inv(noise) @ rgb)
    null_log_ratio = .5 * (null_d2 - thin_best['distance2']
        + np.linalg.slogdet(noise)[1] - np.linalg.slogdet(total)[1])
    bulk_log_ratio = .5 * (bulk_best['distance2'] - thin_best['distance2'])
    optical_match = (thin_best['distance2'] <= MATCH_RADIUS2
                     and null_d2 > MATCH_RADIUS2 and null_log_ratio >= MINIMUM_LOG_RATIO)
    alias = bool(optical_match and bulk_log_ratio < MINIMUM_LOG_RATIO)
    compatible = (distances.min(axis=0) <= MATCH_RADIUS2) & (t > 0) & (t <= 500)
    interval = [float(t[compatible].min()), float(t[compatible].max())] if compatible.any() else None
    supported_target = compatible & target
    if supported_target.any():
        below = bool(np.any(supported_target & (t < 5)))
        above = bool(np.any(supported_target & (t >= 5)))
        band = 'spans_target_bands' if below and above else 'below_5_nm' if below else '5_to_30_nm'
    else:
        band = 'thick_or_bulk' if np.any(compatible & bulk) else 'unknown'
    alternatives = []
    for model in alternative_models:
        at, ad = _profile(rgb, inverse, model)
        ab = _best(at, ad, at > 0)
        if ab is None:
            raise ValueError('Alternative needs positive film thicknesses.')
        alternatives.append({'model_id': model.get('model_id'), 'model_sha256': _model_hash(model),
                             **ab, 'log_ratio_target_vs_alternative': .5 * (ab['distance2'] - thin_best['distance2'])})
    alternative_compatible = any(a['distance2'] <= MATCH_RADIUS2 and
                    a['log_ratio_target_vs_alternative'] < MINIMUM_LOG_RATIO for a in alternatives)
    ambiguous = not alternatives or alternative_compatible
    annotation = operator_annotation or {}
    if annotation and (annotation.get('label') not in ('thin_positive', 'bulk') or not annotation.get('source')):
        raise ValueError('Operator annotation needs explicit label and source provenance.')
    operator_supported = annotation.get('label') == 'thin_positive'
    operator_bulk = annotation.get('label') == 'bulk'
    thin_supported = bool(optical_match and not alias and not operator_bulk)
    passes = bool(thin_supported and not operator_bulk and
                  (operator_supported or (alternatives and not ambiguous)))
    reasons = []
    if not optical_match: reasons.append('thin_branch_not_supported_against_measured_noise_and_substrate')
    if alias: reasons.append('thin_bulk_optical_alias')
    if alternative_compatible: reasons.append('transparent_film_alternative_compatible')
    if not alternatives: reasons.append('transparent_film_alternatives_unassessed')
    if operator_bulk: reasons.append('operator_identified_bulk')
    if operator_supported: reasons.append('operator_thin_positive_not_thickness_calibration')
    return {'policy_version': POLICY_VERSION, 'passes': passes,
            'target_band': 'thick_or_bulk' if operator_bulk else band,
            'thin_supported': thin_supported, 'contamination_ambiguous': bool(ambiguous),
            'contamination_assessed': bool(alternatives), 'operator_supported': bool(operator_supported),
            'operator_annotation': annotation or None, 'alias_ambiguous': alias,
            'thickness_interval_nm': interval, 'reasons': reasons,
            'metrics': {'hbn_model_sha256': _model_hash(hbn_model),
                'best_any': best, 'best_target': thin_best, 'best_bulk': bulk_best,
                'target_vs_bulk_log_ratio': bulk_log_ratio, 'target_vs_substrate_log_ratio': float(null_log_ratio),
                'substrate_distance2': null_d2, 'alternative_fits': alternatives,
                'match_radius2': MATCH_RADIUS2, 'minimum_log_ratio': MINIMUM_LOG_RATIO,
                'model_covariance_rgb': extra.tolist(),
                'compatible_thickness_grid_nm': t[compatible].tolist(),
                'thickness_grid_boundary_reached': bool(compatible[-1]),
                'scenario_method': 'profile each finite scenario, never average RGB curves'},
            'qualification': 'Conditional optical review evidence, not chemical identity or measured thickness. '
                'Thickness interval is a potentially disconnected grid envelope, not a confidence interval. '
                'Finite nuisance scenarios and 20:1 profile-likelihood comparisons are explicit engineering heuristics; '
                'unquantified background/model systematics may dominate. Operator positives are retrieval evidence only.'}
