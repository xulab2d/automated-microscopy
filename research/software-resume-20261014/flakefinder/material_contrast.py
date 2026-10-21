"""RGB material populations with a substrate null and an explicit unknown class.

Gaussian reference populations follow the 2DMatGMM precedent. Their likelihoods
are NOT chemical probabilities. Empirical fits are split by independent source
group, not random pixels; labels and optical context travel with the model.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
from scipy.stats import chi2

from .material_profiles import CONVENTION, canonical_hash
from .mote2_optics import make_pixel_classifier


def covariance(value, name='covariance', positive=True):
    value=np.asarray(value,float)
    if value.shape!=(3,3) or not np.isfinite(value).all() or not np.allclose(value,value.T,atol=1e-12):
        raise ValueError(name+' must be a finite symmetric RGB3x3 matrix.')
    eigen=np.linalg.eigvalsh(value)
    if np.min(eigen)<(-1e-12 if not positive else 1e-15):
        raise ValueError(name+' must be '+('positive definite.' if positive else 'positive semidefinite.'))
    return value


class MaterialContrastClassifier:
    def __init__(self, model, *, context=None, model_covariance=None, allow_context_mismatch=False,
                 null_quantile=.9973, match_quantile=.9973, minimum_log_ratio=np.log(20.)):
        if not 0<null_quantile<1 or not 0<match_quantile<1 or not np.isfinite(minimum_log_ratio):
            raise ValueError('Invalid explicit statistical sieve settings.')
        self.model=model;self.kind=model.get('kind','conditional_optical_curve')
        if model.get('contrast_convention',CONVENTION)!=CONVENTION:raise ValueError('Contrast convention mismatch.')
        self.null_radius2=float(chi2.ppf(null_quantile,3));self.match_radius2=float(chi2.ppf(match_quantile,3))
        self.minimum_log_ratio=float(minimum_log_ratio)
        self.extra=np.zeros((3,3)) if model_covariance is None else covariance(model_covariance,'model covariance',False)
        expected=model.get('applicability_context',{})
        mismatches={key:{'expected':value,'observed':(context or {}).get(key)} for key,value in expected.items() if (context or {}).get(key)!=value}
        if mismatches and not allow_context_mismatch:raise ValueError('Material reference optical context mismatch: '+str(mismatches))
        self.metadata={'schema':1,'model_id':model.get('model_id',model.get('profile_id')),
            'model_sha256':canonical_hash(model),'contrast_convention':CONVENTION,
            'calibration_status':model.get('calibration_status','unvalidated'),
            'context':context or {},'context_mismatches':mismatches,
            'layer_assignment_allowed':False,'chemical_identity_confirmed':False,
            'statistics':{'null_quantile':null_quantile,'match_quantile':match_quantile,'minimum_log_likelihood_ratio':minimum_log_ratio,
                'qualification':'Engineering decision levels under a Gaussian measurement model; not validated precision/recall.'}}
        if self.kind=='empirical_material_populations':
            self.components=model['components']
            if not self.components:raise ValueError('At least one empirical component required.')
            for component in self.components:
                if np.asarray(component['mean_rgb']).shape!=(3,) or not np.isfinite(component['mean_rgb']).all():raise ValueError('Invalid reference mean.')
                covariance(component['covariance_rgb'],'reference population covariance',False)
                if component.get('role') not in ('material','debris','unknown'):raise ValueError('Unknown reference role.')
            self._pixel=None
        else:
            if 'contrast_rgb' not in model:raise ValueError('RGB optical prediction needs an explicit spectral response first.')
            self._pixel=make_pixel_classifier(model,model_covariance=self.extra)
            self.components=None

    def __call__(self, contrast_rgb, noise_covariance):
        return self.classify(contrast_rgb,noise_covariance)

    def classify(self, contrast_rgb, noise_covariance, *, chunk_size=65536):
        x=np.asarray(contrast_rgb,float)
        if x.ndim<1 or x.shape[-1]!=3 or not np.isfinite(x).all():raise ValueError('Finite RGB contrast required.')
        if int(chunk_size)<1:raise ValueError('Positive chunk size required.')
        shape=x.shape[:-1];flat=x.reshape(-1,3);noise=covariance(noise_covariance,'measurement covariance')
        inverse=np.linalg.inv(noise);d0=np.einsum('nc,cd,nd->n',flat,inverse,flat)
        labels=np.zeros(len(flat),np.int16);best=np.full(len(flat),np.inf);ratio=np.full(len(flat),-np.inf)
        ambiguous=np.zeros(len(flat),bool);thickness=np.full(len(flat),np.nan)
        if self._pixel is not None:
            p=self._pixel(flat,noise);best=np.asarray(p['min_mahalanobis_distance']).ravel()**2
            thickness=np.asarray(p['best_conditional_thickness_nm']).ravel()
            # The forward classifier already supplies the exact grid index.
            # Its float32 thickness value can round upward (e.g.0.34nm), so a
            # new searchsorted would silently relabel that pixel as the next bin.
            labels=1+np.asarray(p['conditional_thickness_index']).ravel().astype(np.int16)
            ratio=.5*(d0-best+np.linalg.slogdet(noise)[1]-np.linalg.slogdet(noise+self.extra)[1])
            roles=np.ones(len(flat),np.int8)
        else:
            roles=np.zeros(len(flat),np.int8)
            for begin in range(0,len(flat),int(chunk_size)):
                part=flat[begin:begin+int(chunk_size)];distances=[];scores=[]
                for c in self.components:
                    total=noise+np.asarray(c['covariance_rgb'])+self.extra
                    delta=part-c['mean_rgb'];d=np.einsum('nc,cd,nd->n',delta,np.linalg.inv(total),delta)
                    distances.append(d);scores.append(-.5*(d+np.linalg.slogdet(total)[1]))
                distances=np.asarray(distances).T;scores=np.asarray(scores).T;winner=np.argmax(scores,axis=1);ind=np.arange(len(part))
                end=begin+len(part);labels[begin:end]=winner+1;best[begin:end]=distances[ind,winner]
                ratio[begin:end]=scores[ind,winner]+.5*(d0[begin:end]+np.linalg.slogdet(noise)[1])
                if scores.shape[1]>1:
                    top=np.partition(scores,-2,axis=1)[:,-2:];ambiguous[begin:end]=abs(top[:,1]-top[:,0])<np.log(3.)
                roles[begin:end]=[{'material':1,'debris':2,'unknown':3}[self.components[j]['role']] for j in winner]
        significant=d0>self.null_radius2
        compatible=significant&(best<=self.match_radius2)&(ratio>=self.minimum_log_ratio)
        if self._pixel is not None:compatible&=thickness>0
        labels[~compatible]=-1;labels[~significant]=0
        return {'class_index':labels.reshape(shape),'material_compatible':(compatible&(roles==1)&~ambiguous).reshape(shape),
            'debris_reference_compatible':(compatible&(roles==2)&~ambiguous).reshape(shape),
            'substrate_compatible':(~significant).reshape(shape),'unknown':(significant&~compatible).reshape(shape),
            'ambiguous_class':ambiguous.reshape(shape),'substrate_distance':np.sqrt(d0).reshape(shape),
            'reference_distance':np.sqrt(np.maximum(best,0)).reshape(shape),'log_likelihood_ratio_vs_substrate':ratio.reshape(shape),
            'conditional_thickness_nm':thickness.reshape(shape),'metadata':self.metadata}


def classify_material_contrast(contrast_rgb, model, noise_covariance, **kwargs):
    classifier=model if isinstance(model,MaterialContrastClassifier) else MaterialContrastClassifier(model,**kwargs)
    return classifier.classify(contrast_rgb,noise_covariance)


def fit_empirical_material_model(samples, *, context, model_id, validation_groups=(),
                                 maximum_pixels_per_group=2000, random_seed=0):
    """Fit one full-covariance population per explicit label from balanced groups.

    Each sample has label, role(material/debris/unknown), group_id, source,
    contrast_rgb(N,3). Validation groups never contribute pixels to fitting.
    OAS shrinkage stabilizes covariance; pixel count does not become evidence for
    the number of independent flakes. No labels are inferred from color clusters.
    """
    from sklearn.covariance import OAS
    if not context or not model_id or int(maximum_pixels_per_group)<10:raise ValueError('Context, model ID and >=10 pixels/group cap required.')
    validation_groups=set(map(str,validation_groups));groups={};sources=[]
    for sample in samples:
        group=str(sample['group_id']);label=str(sample['label']);role=sample.get('role','material')
        values=np.asarray(sample['contrast_rgb'],float)
        if values.ndim!=2 or values.shape[1]!=3 or len(values)<10 or not np.isfinite(values).all():raise ValueError('Each labeled source needs >=10 finite RGB samples.')
        if role not in ('material','debris','unknown') or not sample.get('source'):raise ValueError('Explicit role and source provenance required.')
        key=(label,role,group);groups.setdefault(key,[]).append(values)
        sources.append({'label':label,'role':role,'group_id':group,'source':sample['source'],
                        'pixel_count':len(values),'contrast_sha256':hashlib.sha256(np.ascontiguousarray(values).tobytes()).hexdigest()})
    if validation_groups-set(group for _,_,group in groups):
        raise ValueError('Requested validation groups are absent from the supplied labels.')
    labels=sorted(set((label,role) for label,role,group in groups if group not in validation_groups))
    components=[];rng=np.random.default_rng(random_seed)
    for label,role in labels:
        grouped=[np.concatenate(value) for (name,r,g),value in groups.items() if (name,r)==(label,role) and g not in validation_groups]
        if len(grouped)<2:raise ValueError('Each class needs at least two independent training groups; do not duplicate one flake into groups.')
        count=min(int(maximum_pixels_per_group),min(map(len,grouped)))
        balanced=np.concatenate([v[rng.choice(len(v),count,replace=False)] for v in grouped])
        fit=OAS().fit(balanced)
        components.append({'label':label,'role':role,'mean_rgb':fit.location_.tolist(),'covariance_rgb':fit.covariance_.tolist(),
            'independent_training_groups':len(grouped),'balanced_pixels_per_group':count,'covariance_estimator':'OAS shrinkage of balanced group pixels',
            'covariance_is_population_spread_not_mean_error':True,'shrinkage':float(fit.shrinkage_)})
    if not components:raise ValueError('No training classes.')
    model={'schema':1,'kind':'empirical_material_populations','model_id':model_id,'contrast_convention':CONVENTION,
        'components':components,'applicability_context':dict(context),'calibration_status':'labeled_local_population_fit_not_layer_validation',
        'training_sources':sources,'validation_groups':sorted(validation_groups),'validation':[],
        'layer_assignment_allowed':False,'chemical_identity_confirmed':False}
    # Held-out covariance-aware distances report transfer, not pixel-split accuracy.
    for (label,role,group),values in groups.items():
        if group not in validation_groups:continue
        x=np.concatenate(values);reference=next((c for c in components if c['label']==label and c['role']==role),None)
        if reference is None:raise ValueError('Validation class has no trained reference.')
        delta=x-reference['mean_rgb'];inv=np.linalg.pinv(reference['covariance_rgb'])
        d=np.einsum('nc,cd,nd->n',delta,inv,delta)
        model['validation'].append({'group_id':group,'label':label,'pixels':len(x),'median_distance':float(np.sqrt(np.median(d))),
            'fraction_inside_99_percent_population_ellipsoid':float(np.mean(d<=chi2.ppf(.99,3)))})
    model['caveats']=['Training labels and independent group identities are supplied by the operator.','RGB matches are not chemical identification.','Model applies only to its recorded optical/substrate context.','Held-out source-group checks are distinct from real instance detection precision/recall.']
    return model


def save_material_model(model,path,*,managed_output=True):
    if managed_output:
        from .storage import resolve_output_path
        path=resolve_output_path(path)
    else:
        # Explicitly illustrative model/test-research artifacts only. Production
        # labeled models and scientific segmentation outputs use managed storage.
        if model.get('calibration_status')!='illustrative_uncalibrated':
            raise ValueError('Unmanaged output is allowed only for explicitly illustrative optical predictions.')
        path=Path(path)
    if path.exists():raise FileExistsError('Use a new versioned material model path.')
    path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(model,indent=2,allow_nan=False)
    with path.open('x',encoding='utf-8') as stream:stream.write(payload)
    return {'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def import_2dmatgmm_graphene(path):
    """Convert published BGR/(flake-substrate) values to this project's RGB/sign.

    The external graphene fit uses nominal90nm oxide and a different microscope.
    It is a useful starting population, not a portable target-camera calibration.
    """
    path=Path(path);raw=json.loads(path.read_text(encoding='utf-8-sig'));components=[]
    for label,value in raw.items():
        mean=-np.array([value['contrast'][c] for c in 'rgb'])
        cov=np.asarray(value['covariance_matrix'],float)[::-1,::-1]
        covariance(cov,'external covariance')
        components.append({'label':'external_'+str(label)+'L','role':'material','mean_rgb':mean.tolist(),'covariance_rgb':cov.tolist(),
            'source_label':str(label),'layer_assignment_allowed':False})
    return {'schema':1,'kind':'empirical_material_populations','model_id':'2dmatgmm-graphene90-external-rgb-converted-v1',
        'contrast_convention':CONVENTION,'components':components,
        'applicability_context':{'material':'graphene','oxide_nm':90.,'optical_profile_id':'external_2dmatgmm'},
        'calibration_status':'external_domain_prior_requires_local_calibration',
        'source':{'url':'https://github.com/Jaluus/2DMatGMM','publication':'https://doi.org/10.1088/2632-2153/ad2287',
            'file_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'license':'MIT; see adjacent 2DMatGMM-LICENSE.txt',
            'conversion':'mean sign negated; BGR covariance permuted to RGB on both axes; covariance unchanged by global sign inversion'},
        'layer_assignment_allowed':False,'chemical_identity_confirmed':False,
        'caveats':['External90nm-oxide camera population; do not apply to285/300nm as a calibrated threshold.','Explicit context-mismatch override is required for descriptive foreign-domain exploration.']}
