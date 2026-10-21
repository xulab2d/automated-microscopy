"""MaskTerial learned instance masks on saved TIFFs; never opens microscope APIs.

Run with .venv-maskterial/Scripts/python.exe. All display transformations are
explicit and recorded; original 12-bit camera values remain unchanged on disk.
"""
from pathlib import Path
import argparse,hashlib,json,sys,time
import cv2,numpy as np,torch,transformers,yaml

WORKSPACE=Path(__file__).resolve().parents[1]
MODEL_ROOT=WORKSPACE/'external/maskterial-hf'
sys.path.insert(0,str(MODEL_ROOT))
from probe_cpu import load_model
from sparse_inference import predict_candidates,process_memory_bytes


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def atomic_json(path,value):
    path=Path(path);temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8');temp.replace(path)


def prepare_raw_image(path,white_level,display_transform,gamma):
    """OpenCV reads BGR; quantize only after floating-point ADC normalization."""
    raw=cv2.imread(str(path),cv2.IMREAD_UNCHANGED)
    if raw is None or raw.ndim!=3 or raw.shape[2]!=3:raise ValueError(f'Expected a three-channel TIFF: {path}')
    if not np.isfinite(raw).all():raise ValueError('Non-finite camera data.')
    normalized=np.clip(raw.astype(np.float32)/white_level,0,1)
    if display_transform=='gamma':normalized=normalized**gamma
    bgr=np.rint(normalized*255).astype(np.uint8)
    metrics={'raw_dtype':str(raw.dtype),'raw_max':float(raw.max()),'white_level':white_level,
             'saturation_fraction_rgb':np.mean(raw>=white_level,axis=(0,1))[::-1].tolist()}
    return bgr,metrics


def focus_information(path):
    manifest=path.parent.parent/'manifest.json'
    if not manifest.exists():return {'status':'not_recorded','thin_flake_focus_validated':False},True
    data=json.loads(manifest.read_text(encoding='utf-8-sig'));record=data.get('completed',{}).get(path.stem,{})
    focus=record.get('focus')
    if not focus:return {'status':'no_autofocus_at_this_tile','thin_flake_focus_validated':False},True
    return {'status':'field_autofocus_passed' if focus.get('success') else 'autofocus_failed',
            'success':bool(focus.get('success')),'reason':focus.get('reason'),'prominence':focus.get('prominence'),
            'thin_flake_focus_validated':False},not bool(focus.get('success'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path,help='Raw TIFF file or directory of TIFFs')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--weights',type=Path,default=MODEL_ROOT/'synthetic/model_final.pth')
    parser.add_argument('--display-transform',choices=['linear','gamma'],required=True)
    parser.add_argument('--gamma',type=float,default=.6)
    parser.add_argument('--white-level',type=float,help='Override metadata ADC white level; 4095 for the measured 12-bit stream')
    parser.add_argument('--max-side',type=int,default=1536)
    parser.add_argument('--score-threshold',type=float,default=.3)
    parser.add_argument('--threads',type=int,default=3,help='CPU thread budget; 3 leaves acquisition responsive')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    if not 0<args.gamma<=3 or not 0<args.score_threshold<1 or args.max_side<128 or args.threads<1:raise ValueError('Invalid inference parameters.')
    weights=args.weights.resolve();config=yaml.safe_load(weights.with_name('config.yaml').read_text())
    if config['MODEL']['PIXEL_MEAN']!=[107.,148.,85.] or config['MODEL']['PIXEL_STD']!=[33.,34.,40.]:raise ValueError('Checkpoint normalization differs from this verified converter.')
    if config['MODEL']['RESNETS']['DEPTH']!=50 or config['MODEL']['SEM_SEG_HEAD']['NUM_CLASSES']!=1:raise ValueError('Checkpoint architecture is unsupported.')
    sources=[args.input.resolve()] if args.input.is_file() else sorted(p.resolve() for p in args.input.glob('*.tif*') if p.suffix.lower() in ['.tif','.tiff'])
    if not sources:raise ValueError('No TIFF inputs found.')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True);(output/'masks').mkdir(exist_ok=True);(output/'overlays').mkdir(exist_ok=True)
    parameters={'display_transform':args.display_transform,'gamma':args.gamma if args.display_transform=='gamma' else 1.,
                'white_level_override':args.white_level,'max_side':args.max_side,'score_threshold':args.score_threshold,
                'model_input_channels':'BGR','model_pixel_mean':[107,148,85],'model_pixel_std':[33,34,40],
                'mask_coordinates':'original source pixels; logits bilinearly resized before >0 threshold',
                'torch_version':torch.__version__,'transformers_version':transformers.__version__,'threads':args.threads}
    model_info={'name':'MaskTerial Mask2Former R50 instance shape model','checkpoint':str(weights),'checkpoint_sha256':sha256(weights),
                'source':'https://zenodo.org/records/15765516','converter_sha256':sha256(MODEL_ROOT/'probe_cpu.py'),
                'upstream_converter_sha256':sha256(MODEL_ROOT/'upstream_converter.py'),'config_sha256':sha256(weights.with_name('config.yaml')),
                'material_classification':'not performed','conversion_check':'all model tensors mapped; adapted R50 feature parity verified; full original-decoder parity not checked'}
    postprocessor={'name':'class_bound_single_query_v1','sha256':sha256(MODEL_ROOT/'sparse_inference.py'),
                   'formula':'class_probability * mean(mask_probability within positive logits)',
                   'class_bound':'class_probability < threshold cannot produce a retained mask'}
    fingerprint=hashlib.sha256(json.dumps({'model':model_info,'parameters':parameters},sort_keys=True).encode()).hexdigest()
    destination=output/'predictions.json'
    result={'schema':1,'backend':'maskterial_hf_cpu','validation_status':'unvalidated_target_domain','state':'running',
            'fingerprint':fingerprint,'model':model_info,'parameters':parameters,'postprocessor':postprocessor,'images':[]}
    if destination.exists():
        if not args.resume:raise ValueError('Output already contains predictions; select a new output or --resume.')
        result=json.loads(destination.read_text())
        if result['fingerprint']!=fingerprint:
            # Thread count is an execution resource limit, not an image transform.
            # Permit reducing it on resume, while retaining its audit trail.
            old_parameters={k:v for k,v in result['parameters'].items() if k!='threads'}
            new_parameters={k:v for k,v in parameters.items() if k!='threads'}
            if result['model']!=model_info or old_parameters!=new_parameters:
                raise ValueError('Resume model or processing parameters differ.')
            old_threads=result['parameters']['threads']
            for completed_row in result['images']:
                completed_row.setdefault('inference_threads',old_threads)
            result.setdefault('execution_changes',[]).append({'previous_threads':old_threads,'threads':args.threads,'after_images':len(result['images'])})
            result['parameters']=parameters;result['fingerprint']=fingerprint
        result['state']='running'
        result.pop('error',None)
        if result.get('postprocessor')!=postprocessor:
            if result.get('postprocessor') is not None:
                raise ValueError('Resume postprocessor source differs; select a fresh output directory.')
            result.setdefault('execution_changes',[]).append({'previous_postprocessor':result.get('postprocessor',{'name':'dense_query_v0'}),
                                                             'postprocessor':postprocessor,'after_images':len(result['images'])})
            for completed_row in result['images']:
                completed_row.setdefault('postprocessor','dense_query_v0')
            result['postprocessor']=postprocessor
    prior={r['source_image']:r for r in result['images']}
    torch.set_num_threads(args.threads);model=load_model(weights);atomic_json(destination,result)
    try:
        for source in sources:
            if (output/'STOP').exists():
                result['state']='paused';atomic_json(destination,result)
                print('Paused at a field boundary because output/STOP exists.',flush=True)
                return
            source_hash=sha256(source);metadata_path=source.with_suffix('.json')
            metadata=json.loads(metadata_path.read_text(encoding='utf-8-sig')) if metadata_path.exists() else {}
            metadata_hash=sha256(metadata_path) if metadata_path.exists() else None
            if str(source) in prior:
                row=prior[str(source)]
                if row['source_sha256']!=source_hash or row['metadata_sha256']!=metadata_hash or sha256(output/row['masks_file'])!=row['masks_sha256']:raise ValueError('Previously completed image, metadata or masks changed.')
                continue
            white=args.white_level if args.white_level is not None else metadata.get('metadata',{}).get('white_level')
            if white is None or not np.isfinite(white) or white<=0:raise ValueError('Supply --white-level or camera metadata with the ADC white level.')
            bgr,quality=prepare_raw_image(source,float(white),args.display_transform,args.gamma)
            height,width=bgr.shape[:2];scale=min(1,args.max_side/max(height,width));small=cv2.resize(bgr,None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA)
            proposals,diagnostics,seconds=predict_candidates(model,small,args.score_threshold)
            indices=np.array([row['query'] for row in proposals],dtype=np.int32)
            scores=np.array([row['score'] for row in proposals],dtype=np.float32)
            masks=np.stack([cv2.resize(row['logits'],(width,height),interpolation=cv2.INTER_LINEAR)>0 for row in proposals]) if proposals else np.zeros((0,height,width),dtype=bool)
            name=source.stem+'_'+source_hash[:10];mask_path=output/'masks'/(name+'.npz')
            np.savez_compressed(mask_path,masks=masks,scores=scores,query_indices=indices)
            canvas=small.copy()
            for proposal in proposals:
                contours,_=cv2.findContours(np.uint8(proposal['logits']>0),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);cv2.drawContours(canvas,contours,-1,(0,255,255),1)
            overlay=output/'overlays'/(name+'.jpg');cv2.imwrite(str(overlay),canvas)
            focus,limited=focus_information(source)
            row={'tile_id':source.stem,'source_image':str(source),'source_sha256':source_hash,'metadata_sha256':metadata_hash,
                 'original_shape_hw':[height,width],'processed_shape_hw':list(small.shape[:2]),'position':metadata.get('position'),'pixel_size_um':metadata.get('pixel_size_um'),
                 'masks_file':mask_path.relative_to(output).as_posix(),'masks_sha256':sha256(mask_path),'overlay':overlay.relative_to(output).as_posix(),
                 'focus_status':focus,'focus_limited':limited,'quality':quality,'inference_seconds':seconds,'inference_threads':args.threads,
                 'postprocessor':postprocessor['name'],'process_memory_bytes':process_memory_bytes(),
                 'candidates':[{'query_index':int(index),'score':float(scores[j]),'area_px':int(masks[j].sum())} for j,index in enumerate(indices)],
                 'top_scores':sorted([item['score'] for item in diagnostics['evaluated']],reverse=True)[:5],
                 'query_diagnostics':diagnostics}
            result['images'].append(row);atomic_json(destination,result)
            print(json.dumps({'tile':source.stem,'masks':len(indices),'seconds':seconds,'focus_limited':limited}),flush=True)
            # Do not retain 100 image-size float logits while allocating the
            # following field's activations on a microscope control PC.
            del proposals,scores,masks,bgr,small,canvas
        result['state']='complete';atomic_json(destination,result)
    except BaseException as exc:
        result['state']='failed';result['error']=str(exc);atomic_json(destination,result);raise


if __name__=='__main__':main()
