"""Unsupervised RGB-contrast populations; groups are optical colors, not layer labels."""
from dataclasses import dataclass, asdict
import cv2
import numpy as np
from .imaging import background_surface, correct_flat_field
from .model import AcquisitionError

@dataclass
class AnalysisOptions:
    min_area_um2: float = 100
    min_detection_area_um2: float = .25
    max_area_um2: float = 20000
    min_contrast: float = .015
    substrate_sigma: float = 5
    edge_margin_um: float = 12
    max_components: int = 8
    sample_per_tile: int = 5000
    min_solidity: float = .72
    max_perimeter_ratio: float = 8
    max_uniformity: float = .025
    min_posterior: float = .65
    max_color_distance_squared: float = 16
    min_width_um: float = 5
    boundary_probe_um: float = 1.5
    min_boundary_support: float = .6
    min_facet_fraction: float = .4
    max_texture: float = .012
    min_shape_score: float = .5
    spatial_sigma_um: float = .6

    def __post_init__(self):
        if not np.isfinite(list(asdict(self).values())).all(): raise ValueError('Nonfinite analysis options.')
        if not 0 < self.min_area_um2 < self.max_area_um2: raise ValueError('Invalid area interval.')
        if not 0 < self.min_detection_area_um2 <= self.min_area_um2:raise ValueError('Detection area must be positive and no larger than the useful catalog area.')
        if not 0 < self.min_contrast < 1 or self.substrate_sigma < 2 or self.edge_margin_um < 0: raise ValueError('Invalid substrate separation.')
        if not 1 <= self.max_components <= 16 or not 200 <= self.sample_per_tile <= 50000: raise ValueError('Invalid color sampling budget.')
        if not 0 < self.min_solidity <= 1 or not 0 < self.min_posterior < 1 or self.max_uniformity <= 0 or self.max_perimeter_ratio <= 0 or self.max_color_distance_squared <= 0: raise ValueError('Invalid quality filter.')
        if self.min_width_um<=0 or self.boundary_probe_um<=0 or self.spatial_sigma_um<=0 or not 0<=self.min_boundary_support<=1 or not 0<=self.min_facet_fraction<=1 or not 0<=self.min_shape_score<=1 or self.max_texture<=0: raise ValueError('Invalid object evidence thresholds.')


def published_shape_score(perimeter_ratio,solidity):
    """Uslu's learned flake-vs-other shape discriminator, positive-class score.

    Coefficients from 2DMatGMM's classifier_L2_logistic.joblib, commit
    e30c4d60fdb8f1dbbe79c53099261d7f2b67aca9 (MIT, Jan-Lucas Uslu 2023).
    See docs/THIRD_PARTY.md. Exporting numerical coefficients avoids unpickling
    executable model files at runtime. This is not calibrated MoTe2 probability.
    """
    logit=-.6023313994658692*perimeter_ratio+3.407656207829059*solidity+2.960335491687551
    return float(1/(1+np.exp(-np.clip(logit,-700,700))))


def mote2_thin_screen(contrast):
    """Broad optical screening prior, NOT a layer classifier.

    Biswas et al., ACS Nano 2023 / arXiv:2302.03720, Methods and SI Fig A6:
    monolayers on 285 nm oxide have roughly 7% contrast; displayed ML terraces
    darken red/green relative to substrate. Channel gains cancel in normalized
    linear contrast, but spectrum/gamma/oxide variability still prevent direct
    transfer of a precise RGB calibration.
    """
    c=np.asarray(contrast,float)
    if c.shape!=(3,) or not np.isfinite(c).all():return {'score':0.,'status':'unavailable'}
    # The broad width and bounds are declared engineering screening tolerances,
    # not uncertainty intervals measured in that publication.
    direction=bool(c[0]>0 and c[1]>0)
    compatible=direction and .015<=c[1]<=.25 and np.max(np.abs(c))<.6
    score=float(np.exp(-.5*((c[1]-.07)/.07)**2)) if compatible else 0.
    return {'score':score,'status':'thin_screen' if compatible else 'outside_thin_screen',
            'layer_number':None,'reference':'https://arxiv.org/pdf/2302.03720',
            'qualification':'Broad contrast prior only; material and thickness unconfirmed.'}


def regional_contrast(rgb,mask,background_mask,pixel_size_um):
    """Median local-substrate contrast with a conservative spatial standard error.

    The error treats 1 µm-radius patches as independent samples, not individual
    camera pixels. It excludes unknown systematic optical/calibration error.
    """
    values=np.asarray(rgb,dtype=float)
    obj=values[np.asarray(mask,bool)];bg=values[np.asarray(background_mask,bool)]
    if len(obj)<6 or len(bg)<30:return None
    obj_med=np.median(obj,axis=0);bg_med=np.median(bg,axis=0)
    if np.min(bg_med)<=0:return None
    pxarea=float(np.prod(pixel_size_um));correlation_area=max(np.pi,pxarea)
    n_obj=max(1,len(obj)*pxarea/correlation_area);n_bg=max(1,len(bg)*pxarea/correlation_area)
    obj_sigma=1.4826*np.median(np.abs(obj-obj_med),axis=0)
    bg_sigma=1.4826*np.median(np.abs(bg-bg_med),axis=0)
    # Approximate median standard error for a Gaussian core.
    sigma=1.253*np.sqrt(obj_sigma**2/n_obj+(obj_med/bg_med)**2*bg_sigma**2/n_bg)/bg_med
    return {'contrast_rgb':((bg_med-obj_med)/bg_med).tolist(),'standard_error_rgb':sigma.tolist(),
            'effective_object_samples':n_obj,'effective_background_samples':n_bg,
            'substrate_rgb':bg_med.tolist(),'method':'local substrate annulus; robust medians',
            'uncertainty_note':'Spatial sampling error only; excludes calibration/systematic optical uncertainty.'}

def prepare_contrast(frame, options, region=None):
    rgb=correct_flat_field(frame.rgb)
    # Chip geometry is supplied by the acquisition plan. An autofocus brightness
    # mask can mistake bright bulk flakes for substrate on a dark oxide image.
    valid=np.ones(rgb.shape[:2],bool)
    if region is not None: valid &= np.asarray(region,dtype=bool)
    # The specimen edge and its illumination halo are not substrate observations.
    radius=int(np.ceil(options.edge_margin_um/min(frame.pixel_size_um)))
    if radius:
        valid=cv2.erode(np.uint8(valid),np.ones((2*radius+1,2*radius+1),np.uint8)).astype(bool)
    if valid.sum()<1000: raise AcquisitionError('Too little exposed substrate in this field.')
    background=background_surface(rgb,valid)
    contrast=(background-rgb)/np.maximum(background,1)
    smooth=np.stack([cv2.medianBlur(np.float32(contrast[:,:,i]),3) for i in range(3)],axis=2)
    # A useful 100 µm² terrace spans thousands of 20x pixels. Aggregate at a
    # fixed submicrometre scale instead of requiring each noisy camera pixel to
    # independently resolve a few-percent optical contrast.
    sigma=options.spatial_sigma_um/min(frame.pixel_size_um)
    smooth=cv2.GaussianBlur(smooth,(0,0),sigma)
    # Estimate the near-substrate noise ellipsoid, excluding flake tails.
    sample=smooth[valid][::max(1,int(valid.sum()/25000))].astype(float)
    centre=np.median(sample,axis=0)
    deviation=np.linalg.norm(sample-centre,axis=1)
    core=sample[deviation<=np.percentile(deviation,60)]
    covariance=np.cov(core.T)+np.eye(3)*.002**2
    # High-frequency noise is distinct from slow substrate/illumination variation.
    # It sets a measured floor for local edge evidence, not a material class.
    residual=contrast-cv2.GaussianBlur(np.float32(contrast),(0,0),1)
    raw_noise=np.maximum(1.4826*np.median(np.abs(residual[valid]-np.median(residual[valid],axis=0)),axis=0),.001)
    filtered_residual=smooth-cv2.GaussianBlur(smooth,(0,0),max(1,2*sigma))
    noise=np.maximum(1.4826*np.median(np.abs(filtered_residual[valid]-np.median(filtered_residual[valid],axis=0)),axis=0),.001)
    delta=smooth-centre
    distance=np.einsum('...i,ij,...j->...',delta,np.linalg.inv(covariance),delta,optimize=True)
    foreground=valid & (distance>options.substrate_sigma**2) & (np.linalg.norm(delta,axis=2)>options.min_contrast)
    foreground=cv2.morphologyEx(np.uint8(foreground),cv2.MORPH_OPEN,np.ones((3,3),np.uint8)).astype(bool)
    return {'contrast':contrast,'smooth':smooth,'foreground':foreground,'valid':valid,
            'background':background,'substrate_mean':centre,'substrate_covariance':covariance,
            'noise_rgb':noise,'raw_noise_rgb':raw_noise,
            'foreground_fraction':float(foreground.sum()/valid.sum())}

class ColorGroups:
    def __init__(self,data):
        self.data=data
        self.groups=data['groups']

    @classmethod
    def fit(cls,samples,max_components=8):
        values=np.asarray(samples,dtype=np.float64).reshape(-1,3)
        values=values[np.isfinite(values).all(axis=1)]
        if len(values)<30:return cls({'schema':1,'groups':[],'samples':len(values),'status':'insufficient foreground'})
        # A bounded balanced sample prevents a large tile or dust blob dominating
        # computation. Full covariances preserve the RGB color direction.
        rng=np.random.default_rng(42)
        if len(values)>30000:values=values[rng.choice(len(values),30000,replace=False)]
        best=None; scores=[]
        from sklearn.mixture import GaussianMixture
        max_k=min(int(max_components),max(1,len(values)//100),len(np.unique(np.round(values,6),axis=0)))
        for k in range(1,max_k+1):
            em=GaussianMixture(n_components=k,covariance_type='full',reg_covar=.0015**2,
                               random_state=42,n_init=2,max_iter=150).fit(values)
            if not em.converged_:continue
            bic=float(em.bic(values))
            scores.append({'components':k,'bic':bic})
            if best is None or bic<best[0]: best=(bic,em)
        if best is None:raise AcquisitionError('Color-space mixture fit failed.')
        em=best[1];means=em.means_;covs=em.covariances_;weights=em.weights_
        order=np.argsort(np.linalg.norm(means,axis=1)); groups=[]
        for rank,j in enumerate(order):
            groups.append({'id':f'C{rank+1:02d}','mean':means[j].tolist(),
                'covariance':(covs[j]+np.eye(3)*.0015**2).tolist(),'weight':float(weights[j]),
                'contrast_norm':float(np.linalg.norm(means[j]))})
        return cls({'schema':1,'method':'Full-covariance Gaussian mixture, BIC selection',
            'contrast_definition':'(substrate-flake)/substrate','groups':groups,'samples':len(values),'bic':scores,
            'status':'Unsupervised optical populations. Contrast order does not establish layer number.'})

    def predict(self,values):
        x=np.asarray(values,float).reshape(-1,3)
        if not self.groups:return np.full(len(x),-1),np.zeros(len(x)),np.full(len(x),np.inf)
        logp=[]; distances=[]
        for group in self.groups:
            cov=np.array(group['covariance']);d=x-group['mean']
            dist=np.einsum('ni,ij,nj->n',d,np.linalg.inv(cov),d)
            logp.append(-.5*(dist+np.linalg.slogdet(cov)[1])+np.log(max(group['weight'],1e-12)))
            distances.append(dist)
        logp=np.array(logp).T;distances=np.array(distances).T
        label=logp.argmax(axis=1);prob=np.exp(logp-logp.max(axis=1,keepdims=True));prob/=prob.sum(axis=1,keepdims=True)
        return label,prob[np.arange(len(x)),label],distances[np.arange(len(x)),label]
