import importlib.util
from pathlib import Path
import pytest
from types import SimpleNamespace
from test_hbn_review import hbn
from test_graphene_review import offline,save

spec=importlib.util.spec_from_file_location('hbn_controller',Path(__file__).parents[1]/'scripts/run_hbn_offline.py')
controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)


def test_publication_requires_committed_hash_and_exact_material(hbn):
    f=hbn;root=f['reader'].root;sha=controller.digest(f['run']/'scan-request.json')
    selected=controller.analysis_selection(root,f['run_id'],'chip001',f['output'],sha)
    assert selected==f['selector']
    (f['output']/'fields/one/field.json').write_text('{}')
    with pytest.raises(ValueError,match='checksum'):controller.analysis_selection(root,f['run_id'],'chip001',f['output'],sha)


def test_live_refinement_selection_waits_for_valid_generation(hbn):
    f=hbn;root=f['reader'].root;output=f['output'].parent/'refinement-v1';output.mkdir()
    assert controller.review_selection(root,f['run_id'],output,f['selector']) is None
    plan={'run_id':f['run_id'],'source_plan_sha256':f['selector']['plan_sha256']}
    plan_hash=save(output/'plan.json',plan)
    index={'schema':2,'run_id':f['run_id'],'plan_sha256':f['selector']['plan_sha256'],'refinement_plan_sha256':plan_hash}
    index_hash=save(output/'index-000010.json',index)
    latest={'index_path':'index-000010.json','sha256':index_hash,'refinement_plan_sha256':plan_hash}
    save(output/'latest.json',latest)
    selection=controller.review_selection(root,f['run_id'],output,f['selector'])
    assert selection['refinement']=={'kind':'stream','path':output.relative_to(root).as_posix(),'plan_sha256':plan_hash}
    latest['index_path']='../other.json';save(output/'latest.json',latest)
    with pytest.raises(ValueError,match='escapes'):controller.review_selection(root,f['run_id'],output,f['selector'])


def test_new_analysis_preserves_previous_refinement_pointer_evidence(hbn):
    f=hbn;control=f['output'].parent/'controller';control.mkdir()
    previous={'schema':1,'run_id':f['run_id'],'plan_sha256':'old'}
    path=f['run']/'hbn-review-selection-chip001.json';save(path,previous);raw=path.read_bytes()
    controller.publish(f['run'],control,'analysis','chip001',f['selector'])
    assert not path.exists() and (control/('previous-'+path.name)).read_bytes()==raw
    assert controller.read(f['run']/'hbn-analysis-selection-chip001.json')==f['selector']


@pytest.mark.parametrize('defer_selection',[False,True])
def test_controller_runs_both_subprocesses_and_publishes_verified_stream(hbn,monkeypatch,defer_selection):
    f=hbn;root=f['reader'].root;template=root/'template';f['output'].rename(template)
    scripts=root/'fake-scripts';scripts.mkdir()
    program='''import argparse,hashlib,json,pathlib,shutil
p=argparse.ArgumentParser()
for name in ('project','run-id','chip','version','model','tiles','workers','analysis'):p.add_argument('--'+name)
a=p.parse_args()
assert a.workers=='2' and a.tiles=='all'
def save(path,value):
 path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value));return hashlib.sha256(path.read_bytes()).hexdigest()
if a.analysis:
 source=pathlib.Path(a.analysis);out=source.parent/a.version;out.mkdir()
 source_hash=hashlib.sha256((source/'plan.json').read_bytes()).hexdigest()
 run=json.loads((source/'plan.json').read_bytes())['pin']['identity']['run_id']
 ph=save(out/'plan.json',{'run_id':run,'source_plan_sha256':source_hash})
 ih=save(out/'index-000002.json',{'schema':2,'run_id':run,'plan_sha256':source_hash,'refinement_plan_sha256':ph})
 save(out/'latest.json',{'index_path':'index-000002.json','sha256':ih,'refinement_plan_sha256':ph})
else:
 root=pathlib.Path(a.project);out=root/'analysis/hbn'/a.run_id/a.chip/a.version
 shutil.copytree(root/'template',out)
save(out/'status.json',{'state':'complete','finished_fields':2})
'''
    for name in ('analyze_graphene_run.py','refine_graphene_regions.py'):(scripts/name).write_text(program)
    monkeypatch.setattr(controller,'SCRIPTS',scripts)
    args=SimpleNamespace(project=root,run_id=f['run_id'],chip='chip001',model=template/'model.json',
        version='new-analysis',refinement_version='new-refinement',defer_selection=defer_selection)
    phases=[];original_publish=controller.publish
    def observed_publish(run,control,name,chip,selector):
        phases.append((name,controller.read(control/'status.json')['phase']))
        return original_publish(run,control,name,chip,selector)
    monkeypatch.setattr(controller,'publish',observed_publish)
    result=controller.run(args)
    assert result['state']=='complete' and result['analysis_published'] and result['refinement_published']
    assert controller.read(f['run']/'hbn-analysis-selection-chip001.json')['analysis_relative_path'].endswith('/new-analysis')
    review=controller.read(f['run']/'hbn-review-selection-chip001.json')
    assert review['refinement']['kind']=='stream' and review['refinement']['path'].endswith('/new-refinement')
    assert phases[0]==('analysis','refinement' if defer_selection else 'segmentation')
    with pytest.raises(ValueError,match='fresh'):controller.run(args)
