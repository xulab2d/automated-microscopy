"""Offline material profiles, labeled calibration and pixelwise terrace analysis."""
from pathlib import Path
import argparse
import json
import os
import sys

for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ.setdefault(key,'1')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    predict=commands.add_parser('predict');predict.add_argument('--profile',required=True,type=Path)
    predict.add_argument('--thickness-nm',required=True,nargs='+',type=float);predict.add_argument('--oxide-nm',type=float)
    predict.add_argument('--response',type=Path);predict.add_argument('--illustrative',action='store_true');predict.add_argument('--output',required=True,type=Path)
    fit=commands.add_parser('fit');fit.add_argument('--samples',required=True,type=Path);fit.add_argument('--output',required=True,type=Path)
    prepare=commands.add_parser('prepare-input',help='Prepare raw TIFF and independent saved chip support; never uses JPEG colors.')
    prepare.add_argument('--chip',type=Path);prepare.add_argument('--tile');prepare.add_argument('--tiff',type=Path)
    prepare.add_argument('--domain-mask',type=Path);prepare.add_argument('--substrate-mask',type=Path)
    prepare.add_argument('--parent-labels',type=Path);prepare.add_argument('--parent-key',default='labels')
    prepare.add_argument('--context',required=True,type=Path);prepare.add_argument('--output',required=True,type=Path)
    prepare.add_argument('--edge-margin-um',type=float,default=12.)
    segment=commands.add_parser('segment');segment.add_argument('--input-npz',required=True,type=Path)
    segment.add_argument('--model',required=True,type=Path);segment.add_argument('--context',type=Path)
    segment.add_argument('--allow-context-mismatch',action='store_true');segment.add_argument('--output',required=True,type=Path)
    segment.add_argument('--allow-illustrative-model',action='store_true',help='Explicitly explore an uncalibrated illustrative curve, not calibrated material detection.')
    segment.add_argument('--useful-area-um2',type=float,default=25.)
    args=parser.parse_args()
    read=lambda path:json.loads(path.read_text(encoding='utf-8-sig'))
    from flakefinder.material_contrast import save_material_model
    if args.command=='predict':
        from flakefinder.material_profiles import load_material_profile,predict_material_contrast
        result=predict_material_contrast(load_material_profile(args.profile),args.thickness_nm,
            response=read(args.response) if args.response else None,illustrative=args.illustrative,oxide_nm=args.oxide_nm)
        print(json.dumps(save_material_model(result,args.output,managed_output=not args.illustrative)));return
    if args.command=='fit':
        from flakefinder.material_contrast import fit_empirical_material_model
        values=read(args.samples)
        model=fit_empirical_material_model(values['samples'],context=values['context'],model_id=values['model_id'],validation_groups=values.get('validation_groups',()))
        print(json.dumps(save_material_model(model,args.output)));return
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    from flakefinder.material_contrast import MaterialContrastClassifier
    from flakefinder.material_terraces import segment_material_terraces,MaterialTerraceOptions
    from flakefinder.storage import resolve_output_path
    args.output=resolve_output_path(args.output)
    if args.output.exists():raise FileExistsError('Use a new versioned result directory.')
    if args.command=='prepare-input':
        from flakefinder.material_input import prepare_material_input
        arrays,provenance=prepare_material_input(context=read(args.context),tiff=args.tiff,domain_mask=args.domain_mask,
            substrate_mask=args.substrate_mask,chip_folder=args.chip,tile_id=args.tile,parent_labels=args.parent_labels,
            parent_key=args.parent_key,edge_margin_um=args.edge_margin_um)
        args.output.mkdir(parents=True)
        path=args.output/'input.npz';np.savez_compressed(path,**arrays)
        from flakefinder.terrace_domain import digest
        provenance['input_npz_sha256']=digest(path)
        save_material_model(provenance,args.output/'input.json')
        print(json.dumps({'input_npz':str(path),'provenance':str(args.output/'input.json'),'seconds':provenance['seconds']}));return
    model=read(args.model)
    if model.get('calibration_status')=='illustrative_uncalibrated' and not args.allow_illustrative_model:
        raise ValueError('Illustrative RGB is not calibrated. Supply a response/local fit, or explicitly use --allow-illustrative-model for exploratory review.')
    evidence_path=args.input_npz.with_suffix('.json')
    evidence=read(evidence_path) if evidence_path.is_file() else None
    if evidence:
        from flakefinder.terrace_domain import digest
        if evidence.get('input_npz_sha256')!=digest(args.input_npz):raise ValueError('Prepared input differs from its provenance receipt.')
    context=read(args.context) if args.context else (evidence or {}).get('context',{})
    if evidence and context!=evidence['context']:raise ValueError('Segmentation context differs from prepared raw input; prepare a new version with the correct context.')
    for key in ('material','oxide_nm'):
        if key in model and model[key]!=context.get(key):raise ValueError('Optical model does not match input '+key+'.')
    classifier=MaterialContrastClassifier(model,context=context,allow_context_mismatch=args.allow_context_mismatch)
    with np.load(args.input_npz,allow_pickle=False) as data:
        result=segment_material_terraces(data['contrast_rgb'],data['valid_mask'],data['substrate_mask'],data['noise_covariance'],data['pixel_size_um'],classifier,
            parent_labels=data['parent_labels'] if 'parent_labels' in data else None,
            saturated_mask=data['saturated_mask'] if 'saturated_mask' in data else None,
            background_systematic_covariance=data['background_systematic_covariance'] if 'background_systematic_covariance' in data else None,
            options=MaterialTerraceOptions(useful_area_um2=args.useful_area_um2))
    result['report']['prepared_input_provenance']=evidence
    result['report']['illustrative_model_explicit_override']=args.allow_illustrative_model
    args.output.mkdir(parents=True)
    np.savez_compressed(args.output/'masks.npz',**{key:result[key] for key in ('labels','parent_labels','class_map','material_compatible_mask','debris_compatible_mask')})
    save_material_model({'report':result['report'],'regions':result['regions']},args.output/'analysis.json')
    print(json.dumps(result['report'],indent=2))


if __name__=='__main__':main()
