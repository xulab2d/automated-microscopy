"""Read-only raw-image boundary replay; outputs are separate research derivatives."""
from pathlib import Path
import argparse,json,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'research/software-next-20261009'))
from flakefinder.imaging import load_frame
from flakefinder.layout import SubstrateReference,SUBSTRATE,NON_SUBSTRATE,UNKNOWN
from flakefinder.chip_discovery import BoundaryOptions,classify_boundary
from flakefinder.vendor_lifecycle import file_hash
from flakefinder.sparse_edges import validated_coherent_edge
from flakefinder.rectangle_edges import validate_side_materials,measure_rectangle_side


def fractions(labels):
    return {name:float(np.mean(labels==value)) for name,value in
            [('substrate',SUBSTRATE),('stage',NON_SUBSTRATE),('unknown',UNKNOWN)]}


def strip_report(frame,labels,matrix,edge):
    h,w=labels.shape;sh,sw=frame.rgb.shape[:2]
    yy,xx=np.mgrid[:h,:w]
    pixels=np.stack([(xx+.5)*sw/w-sw/2,(yy+.5)*sh/h-sh/2],axis=-1)
    offsets=pixels@np.asarray(matrix).T
    normal=np.asarray(edge['normal']); tangent=np.array([-normal[1],normal[0]])
    distance=offsets@normal+np.array([frame.position.x,frame.position.y])@normal-edge['offset_um']
    along=offsets@tangent
    inside=(distance<=-6)&(distance>=-30);outside=(distance>=6)&(distance<=30)
    bins=np.linspace(float(along.min()),float(along.max()),17)
    rows=[]
    for low,high in zip(bins[:-1],bins[1:]):
        a=inside&(along>=low)&(along<high);b=outside&(along>=low)&(along<high)
        if min(a.sum(),b.sum())<20:continue
        af,bf=fractions(labels[a]),fractions(labels[b])
        rows.append({'tangent_um':[float(low),float(high)],'inside':af,'outside':bf,
            'supported':bool(af['substrate']>=.2 and af['stage']<=.2 and bf['stage']>=.65 and bf['substrate']<=.2)})
    good=[r for r in rows if r['supported']]
    return {'inside':fractions(labels[inside]),'outside':fractions(labels[outside]),
        'bins':rows,'supported_bins':len(good),'sampled_bins':len(rows),
        'supported_span_um':good[-1]['tangent_um'][1]-good[0]['tangent_um'][0] if good else 0.}


def replay(run,output):
    folder=run/'discovery/candidate001'
    saved=json.loads((folder/'discovery.json').read_bytes())
    ref=SubstrateReference.from_frame(load_frame(run/'calibration/substrate.tiff'))
    assert ref.stage_background is not None
    options=BoundaryOptions(saved['options']['search_bounds_um'])
    result={'schema':1,'run_id':run.name,'discovery_sha256':file_hash(folder/'discovery.json'),
        'boundary_source_sha256':file_hash(ROOT/'research/software-next-20261009/flakefinder/chip_discovery.py'),
        'model_sha256':ref.stage_background['model_sha256'],'rows':[],
        'qualification':'Local strip thresholds are research diagnostics, not replacement raw edge validation. No acquisition source changed.'}
    for index in [1,9,12,19]:
        record=saved['records'][index]
        for name,digest in record['hashes'].items():assert file_hash(folder/name)==digest,name
        frame=load_frame(folder/record['image'])
        old=ref.classify(frame);labels=classify_boundary(frame,ref,saved['reference_rgb'],options)
        edge,report=validated_coherent_edge(frame,labels,np.asarray(saved['transform']['matrix']),saved['edge_validation_profile'])
        side,side_report=measure_rectangle_side(frame,saved['transform']['matrix'],record['edge']['normal'],labels=labels)
        stored_support=validate_side_materials(frame,saved['transform']['matrix'],record['edge'],labels)
        row={'probe':index,'phase':record['phase'],'source_hashes':record['hashes'],
            'generic_labels':fractions(old),'boundary_labels':fractions(labels),
            'substrate_unchanged':bool(np.array_equal(old==SUBSTRATE,labels==SUBSTRATE)),
            'local_strips':strip_report(frame,labels,saved['transform']['matrix'],record['edge']),
            'stored_edge_material_support':stored_support,
            'rectangle_raw_accepted':side is not None,'rectangle_raw_validation':side_report,
            'primary_raw_edge_accepted':edge is not None,'primary_raw_validation':report}
        result['rows'].append(row)
        print(json.dumps({k:v for k,v in row.items() if k not in ['primary_raw_validation','rectangle_raw_validation','source_hashes','local_strips']})+
              ' strips='+json.dumps({k:v for k,v in row['local_strips'].items() if k!='bins'}),flush=True)
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();replay(args.run,args.output)
