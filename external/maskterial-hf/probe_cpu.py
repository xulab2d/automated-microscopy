"""Isolated MaskTerial -> Hugging Face CPU feasibility probe; no microscope access.

Uses decoder weight mapping from the Apache-2.0 upstream HF converter next to
this file. ResNet mapping is explicit, and missing model parameters abort.
"""
from pathlib import Path
import ast,argparse,json,logging,re,time
from pprint import pformat
import numpy as np
import torch
from torch import nn
from transformers import Mask2FormerConfig,Mask2FormerForUniversalSegmentation,ResNetConfig

ROOT=Path(__file__).resolve().parent


def converter_types():
    source=ast.parse((ROOT/'upstream_converter.py').read_text(encoding='utf-8'))
    names={'TrackedStateDict','OriginalMask2FormerCheckpointToOursConverter'}
    nodes=[n for n in source.body if isinstance(n,ast.ClassDef) and n.name in names]
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+nodes,type_ignores=[])
    ns={'torch':torch,'nn':nn,'logger':logging.getLogger('conversion'),'pformat':pformat}
    exec(compile(ast.fix_missing_locations(module),'upstream_converter.py','exec'),ns)
    return ns['TrackedStateDict'],ns['OriginalMask2FormerCheckpointToOursConverter']


def load_model(weights=None):
    backbone=ResNetConfig(num_channels=3,embedding_size=64,hidden_sizes=[256,512,1024,2048],depths=[3,4,6,3],
                          layer_type='bottleneck',hidden_act='relu',downsample_in_first_stage=False,
                          out_features=['stage1','stage2','stage3','stage4'])
    cfg=Mask2FormerConfig(backbone_config=backbone,num_labels=1,id2label={0:'flake'},label2id={'flake':0},
                         feature_size=256,mask_feature_size=256,hidden_dim=256,encoder_layers=6,decoder_layers=10,
                         encoder_feedforward_dim=1024,dim_feedforward=2048,num_attention_heads=8,num_queries=100,
                         dropout=0.,pre_norm=False,feature_strides=[4,8,16,32],enforce_input_projection=False)
    model=Mask2FormerForUniversalSegmentation(cfg)
    checkpoint=torch.load(weights or ROOT/'synthetic/model_final.pth',map_location='cpu',weights_only=True)['model']
    Tracked,Original=converter_types()

    class Converter(Original):
        def replace_swin_backbone(self,dst,src,config):
            renames=[]
            for key in list(src):
                if not key.startswith('backbone.'):continue
                tail=key[len('backbone.'):]
                if tail.startswith('stem.conv1.'):
                    tail=tail[len('stem.conv1.'):]
                    dest='embedder.embedder.'+('normalization.'+tail[5:] if tail.startswith('norm.') else 'convolution.'+tail)
                else:
                    match=re.fullmatch(r'res([2-5])\.(\d+)\.(conv([1-3])|shortcut)\.(.+)',tail)
                    if match is None:raise ValueError('Unknown ResNet weight '+key)
                    stage,block,operation,conv,suffix=match.groups()
                    prefix=f'encoder.stages.{int(stage)-2}.layers.{block}.'
                    op='shortcut.' if operation=='shortcut' else f'layer.{int(conv)-1}.'
                    dest=prefix+op+('normalization.'+suffix[5:] if suffix.startswith('norm.') else 'convolution.'+suffix)
                renames.append((key,'pixel_level_module.encoder.'+dest))
            self.pop_all(renames,dst,src)

    converter=Converter(None,cfg)
    target=Tracked(model.model.state_dict());source=dict(checkpoint)
    converter.replace_pixel_module(target,source)
    converter.replace_transformer_module(target,source)
    missing=sorted(target.diff())
    meaningful=[k for k in missing if not k.endswith('num_batches_tracked')]
    if meaningful:raise ValueError('Unmapped HF parameters: '+repr(meaningful))
    model.model.load_state_dict(target.copy(),strict=True)
    model.class_predictor.load_state_dict({x:source.pop('sem_seg_head.predictor.class_embed.'+x) for x in ['weight','bias']})
    if set(source)!={'criterion.empty_weight'}:raise ValueError('Unmapped source parameters: '+repr(list(source)))
    print(json.dumps({'mapped_tensors':len(checkpoint)-1,'initialized_batch_counters_only':len(missing),'parameters':sum(x.numel() for x in model.parameters())}),flush=True)
    return model.eval()


def predict(model,bgr,return_logits=False):
    height,width=bgr.shape[:2]
    data=torch.from_numpy(np.ascontiguousarray(bgr.transpose(2,0,1))).float()
    data=(data-torch.tensor([107.,148.,85.])[:,None,None])/torch.tensor([33.,34.,40.])[:,None,None]
    data=nn.functional.pad(data,(0,(-width)%32,0,(-height)%32))
    start=time.perf_counter()
    with torch.inference_mode():out=model(pixel_values=data[None])
    duration=time.perf_counter()-start
    # Original MaskTerial instance inference: class probability * average foreground mask probability.
    logits=nn.functional.interpolate(out.masks_queries_logits,size=data.shape[-2:],mode='bilinear',align_corners=False)[0,:,:height,:width]
    masks=logits>0;prob=logits.sigmoid();classes=out.class_queries_logits.softmax(-1)[0,:,0]
    scores=classes*(prob*masks).flatten(1).sum(1)/(masks.flatten(1).sum(1)+1e-6)
    return (logits.numpy() if return_logits else masks.numpy()),scores.numpy(),duration


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--image');p.add_argument('--max-side',type=int,default=768);p.add_argument('--output',default=str(ROOT/'probe-output'));p.add_argument('--threads',type=int,default=8);p.add_argument('--weights')
    args=p.parse_args();torch.set_num_threads(args.threads);model=load_model(args.weights)
    if args.image:
        import cv2
        image=cv2.imread(args.image,cv2.IMREAD_COLOR)
        if image is None:raise ValueError(args.image)
        scale=min(1,args.max_side/max(image.shape[:2]));image=cv2.resize(image,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
        masks,scores,seconds=predict(model,image)
        output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
        np.savez_compressed(output/'predictions.npz',masks=masks,scores=scores)
        canvas=image.copy();rows=[]
        for index in np.argsort(-scores):
            if scores[index]<.5:continue
            contours,_=cv2.findContours(masks[index].astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(canvas,contours,-1,(0,255,255),1)
            rows.append({'query':int(index),'score':float(scores[index]),'area_pixels':int(masks[index].sum())})
        cv2.imwrite(str(output/'overlay.jpg'),canvas)
        result={'source_image':args.image,'scale':scale,'shape':list(image.shape),'seconds':seconds,'instances':rows,'top_scores':sorted(scores.tolist(),reverse=True)[:10],
                'status':'research inference; conversion parity and target-domain accuracy not validated'}
        (output/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
