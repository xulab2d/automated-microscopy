"""Run-specific operator RGB trajectory proposals, never layer assignments."""
import hashlib
import numpy as np
from .material_contrast import MaterialContrastClassifier, covariance
from .material_profiles import CONVENTION


def build_model(observations, context, observation_sha256):
    first=[x for x in observations['first_region']['consistency'] if x['below_reported_line']]
    negative_ids=set(observations['second_region']['inferred_outlier_ids'])
    second=[x for x in observations['second_region']['saved_points'] if x['id'] not in negative_ids]
    third=observations['third_region']['saved_points']
    negative=[x for x in observations['second_region']['saved_points'] if x['id'] in negative_ids]
    positive=first+second+third
    values=np.array([x['contrast_rgb'] for x in positive])
    direction=np.r_[1.,np.median(values[:,1:]/values[:,:1],axis=0)]
    residual=values-values[:,:1]*direction
    spread=np.cov(residual.T)+np.eye(3)*.006**2
    bad=np.array([x['contrast_rgb'] for x in negative])
    components=[{'label':'operator_supported_rgb_trajectory','role':'material','mean_rgb':values.mean(0).tolist(),'covariance_rgb':spread.tolist()},
        {'label':'operator_negative_achromatic_population','role':'debris','mean_rgb':bad.mean(0).tolist(),'covariance_rgb':(np.cov(bad.T)+np.eye(3)*.006**2).tolist()},
        {'label':'extrapolated_rgb_trajectory_unconfirmed','role':'unknown','mean_rgb':values.mean(0).tolist(),'covariance_rgb':spread.tolist()}]
    return {'schema':1,'kind':'empirical_material_populations','model_id':'graphene221727-operator-trajectory-v1',
        'contrast_convention':CONVENTION,'applicability_context':dict(context),
        'calibration_status':'operator_band_supported_run_specific_proposals_not_independent_instance_validation',
        'components':components,'empirical_trajectory':{'direction_rgb':direction.tolist(),'origin_rgb':[0.,0.,0.],
            'supported_red_interval':[max(0.,float(values[:,0].min())-.01),float(values[:,0].max())+.01],
            'supported_interval_margin_rgb':.01,
            'first_band':{'red_interval':[.10,.15],'green_slope':.6,'green_intercept':.03,'transition_margin':.003},
            'proposal_red_interval':[0.,.51],'residual_covariance_rgb':spread.tolist(),
            'negative_preference_margin_distance2':float(2*np.log(3)),
            'covariance_floor_rgb':.006,'method':'noise-weighted continuous RGB projection; three empirical bands support interpolation',
            'qualification':'Engineering proposal corridor, not a calibrated probability. Mixed boundary pixels may also match. Extrapolation is retained as unknown.'},
        'observed_populations':[{'name':name,'mean_rgb':np.mean([x['contrast_rgb'] for x in rows],axis=0).tolist(),'observation_ids':[x['id'] for x in rows]} for name,rows in [('first',first),('second',second),('third',third)]],
        'source':{'run_id':observations['run_id'],'operator_observations_sha256':observation_sha256,
            'negative_observation_ids':sorted(negative_ids),'exact_point_correspondence':'inferred from recorded measurements; operator confirmed contrast bands, not independent point labels'},
        'layer_assignment_allowed':False,'chemical_identity_confirmed':False,
        'qualification':'Run-specific operator RGB candidate proposals; other optical features retained. No layer or chemical assignment.'}


class EmpiricalGrapheneClassifier(MaterialContrastClassifier):
    def __init__(self, model, **kwargs):
        super().__init__(model,**kwargs)
        t=model['empirical_trajectory'];self.trajectory=t
        self.direction=np.asarray(t['direction_rgb'],float);self.origin=np.asarray(t['origin_rgb'],float)
        self.spread=covariance(t['residual_covariance_rgb'],positive=False)
        if self.direction.shape!=(3,) or not np.isfinite(self.direction).all() or self.direction[0]!=1:raise ValueError('Invalid RGB direction.')
        lo,hi=t['supported_red_interval'];plo,phi=t['proposal_red_interval']
        if not 0<=plo<=lo<hi<=phi:raise ValueError('Invalid empirical amplitude intervals.')
        self.metadata['empirical_trajectory']=t
        self.metadata['qualification']=model['qualification']

    def classify(self, contrast_rgb, noise_covariance, *, chunk_size=65536):
        x=np.asarray(contrast_rgb,float)
        if x.shape[-1]!=3 or not np.isfinite(x).all():raise ValueError('Finite RGB contrast required.')
        noise=covariance(noise_covariance);shape=x.shape[:-1];flat=x.reshape(-1,3);delta=flat-self.origin
        total=noise+self.spread+self.extra;inverse=np.linalg.inv(total)
        amplitude=(delta@inverse@self.direction)/(self.direction@inverse@self.direction)
        residual=delta-amplitude[:,None]*self.direction
        distance=np.einsum('nc,cd,nd->n',residual,inverse,residual)
        null=np.einsum('nc,cd,nd->n',flat,np.linalg.inv(noise),flat)
        ratio=.5*(null-distance+np.linalg.slogdet(noise)[1]-np.linalg.slogdet(total)[1])
        bad=self.components[1];bd=flat-bad['mean_rgb'];bt=noise+np.asarray(bad['covariance_rgb'])+self.extra
        bad_distance=np.einsum('nc,cd,nd->n',bd,np.linalg.inv(bt),bd)
        significant=null>self.null_radius2
        compatible=significant&(distance<=self.match_radius2)&(ratio>=self.minimum_log_ratio)
        # Large measurement uncertainty must not widen the empirical colour corridor.
        envelope_inverse=np.linalg.inv(self.spread+self.extra)
        envelope_amplitude=(delta@envelope_inverse@self.direction)/(self.direction@envelope_inverse@self.direction)
        envelope_residual=delta-envelope_amplitude[:,None]*self.direction
        envelope_distance=np.einsum('nc,cd,nd->n',envelope_residual,envelope_inverse,envelope_residual)
        distance=np.maximum(distance,envelope_distance)
        compatible &= envelope_distance<=self.match_radius2
        band=self.trajectory['first_band'];red=flat[:,0]
        in_first=(red>=band['red_interval'][0])&(red<=band['red_interval'][1])
        above_first=flat[:,1]>band['green_slope']*red+band['green_intercept']+band['transition_margin']
        compatible &= ~(in_first&above_first)
        negative=significant&(bad_distance<=self.match_radius2)&(bad_distance+self.trajectory['negative_preference_margin_distance2']<distance)
        compatible &= ~negative
        lo,hi=self.trajectory['supported_red_interval'];plo,phi=self.trajectory['proposal_red_interval']
        supported=compatible&(amplitude>=lo)&(amplitude<=hi)
        extrapolated=compatible&~supported&(amplitude>=plo)&(amplitude<=phi)
        labels=np.full(len(flat),-1,np.int16);labels[~significant]=0;labels[supported]=1;labels[negative]=2;labels[extrapolated]=3
        # Optical ranking must measure distance from material, not closeness to a negative reference.
        return {'class_index':labels.reshape(shape),'material_compatible':supported.reshape(shape),
            'debris_reference_compatible':negative.reshape(shape),'substrate_compatible':(~significant).reshape(shape),
            'unknown':(significant&~supported&~negative).reshape(shape),'ambiguous_class':np.zeros(shape,bool),
            'substrate_distance':np.sqrt(null).reshape(shape),'reference_distance':np.sqrt(np.maximum(distance,0)).reshape(shape),
            'log_likelihood_ratio_vs_substrate':ratio.reshape(shape),'conditional_thickness_nm':np.full(shape,np.nan),
            'metadata':self.metadata}
