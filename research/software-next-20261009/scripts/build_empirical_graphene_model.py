"""Create a new run-pinned operator trajectory model; no acquisition access."""
import argparse,hashlib,json
from pathlib import Path
import analyze_graphene_run as analysis
from flakefinder.graphene_empirical import build_model
from flakefinder.storage import resolve_output_path

def build(project,run_id,chip,observations,version):
    source=analysis.read(observations)
    if source['run_id']!=run_id:raise ValueError('Operator observations belong to another run.')
    run,_=analysis.watcher.request_identity(project,run_id)
    manifest=analysis.read(run/chip/'manifest.json')
    entry=analysis.field_entry(run/chip,'tile_000639',manifest)
    context=analysis.watcher.input_context(entry,run_id)
    model=build_model(source,context,analysis.digest(observations))
    model['source']['request_sha256']=analysis.digest(run/'scan-request.json')
    model['source']['manifest_sha256']=analysis.digest(run/chip/'manifest.json')
    output=resolve_output_path(project/'analysis/graphene'/run_id/chip/version/'model.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:json.dump(model,stream,indent=2,allow_nan=False)
    return output

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--run-id',required=True)
    p.add_argument('--chip',default='chip001');p.add_argument('--observations',type=Path,required=True);p.add_argument('--version',required=True)
    a=p.parse_args();print(build(a.project,a.run_id,a.chip,a.observations,a.version))
