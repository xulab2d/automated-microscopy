"""One sequential offline owner for an explicitly versioned operator derivative."""
import argparse,json,subprocess,sys,time
from pathlib import Path
import analyze_graphene_run as driver

def run(a):
    project=a.project.resolve();run=project/'data'/a.run_id;base=project/'analysis/graphene'/a.run_id/a.chip
    analysis=base/a.version;analysis.mkdir(parents=True,exist_ok=True)
    status_path=analysis/'pipeline-status.json';status={'run_id':a.run_id,'chip':a.chip,'no_acquisition_performed':True}
    def report(**kw):status.update(kw,updated_at=time.time());driver.watcher.save(status_path,status)
    def stage(name,script,args,folder):
        report(state='running',stage=name)
        while True:
            if (analysis/'PIPELINE_STOP').exists():raise RuntimeError('Offline pipeline STOP requested')
            with (analysis/(name+'-stdout.txt')).open('a',encoding='utf-8') as out,(analysis/(name+'-stderr.txt')).open('a',encoding='utf-8') as err:
                process=subprocess.Popen([sys.executable,str(Path(__file__).parent/script),*map(str,args)],stdout=out,stderr=err)
                report(child_pid=process.pid)
                while process.poll() is None:
                    if (analysis/'PIPELINE_STOP').exists():
                        (folder/('DEDUP_STOP' if name=='grouping' else 'STOP')).touch(exist_ok=True)
                    time.sleep(1)
                code=process.returncode
            result=driver.read(folder/('dedup-progress.json' if name=='grouping' else 'status.json'));report(stage_status=result)
            if result.get('state')=='resource_paused':report(state='waiting_for_memory');time.sleep(30);continue
            if code or result.get('state') not in ('complete','completed'):raise RuntimeError(name+' did not complete: '+str(result))
            if name=='segmentation' and any(result.get('counts',{}).get(key,0) for key in ('failed','invalid_source','budget_exhausted')):
                raise RuntimeError('Unexpected segmentation failures require review before selection: '+str(result['counts']))
            return result
    def select(refine=None,dedup=None):
        sha=driver.digest(analysis/'plan.json')
        selection={'schema':1,'run_id':a.run_id,'analysis_relative_path':analysis.relative_to(project).as_posix(),'plan_sha256':sha}
        review={'schema':1,'run_id':a.run_id,'plan_sha256':sha}
        if refine:review['refinement']={'kind':'stream','path':refine.relative_to(project).as_posix(),'plan_sha256':driver.digest(refine/'plan.json')}
        if dedup:review['dedup']={'path':dedup.relative_to(project).as_posix(),'sha256':driver.digest(dedup)}
        history=analysis/'prior-selections';history.mkdir(exist_ok=True)
        for name,value in [(f'graphene-analysis-selection-{a.chip}.json',selection),('graphene-analysis-selection.json',selection),
                (f'graphene-review-selection-{a.chip}.json',review),('graphene-review-selection.json',review)]:
            path=run/name;backup=history/name
            if path.exists() and not backup.exists():
                with backup.open('xb') as stream:stream.write(path.read_bytes())
            driver.watcher.save(path,value)
    try:
        stage('segmentation','analyze_graphene_run.py',['--project',project,'--run-id',a.run_id,'--chip',a.chip,'--version',a.version,'--model',a.model,'--workers','2'],analysis)
        refined=base/(a.version+'-boundaries')
        stage('boundaries','refine_graphene_regions.py',['--analysis',analysis,'--version',refined.name],refined)
        select(refine=refined)
        grouped=base/(a.version+'-review')
        stage('grouping','group_saved_graphene.py',['--project',project,'--run-id',a.run_id,'--chip',a.chip,'--version',grouped.name],grouped)
        select(refine=refined,dedup=grouped/'dedup.json')
        report(state='complete',stage='review_ready',child_pid=None)
    except BaseException as exc:report(state='failed',reason=str(exc));raise
    return status

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--run-id',required=True)
    p.add_argument('--chip',default='chip001');p.add_argument('--version',required=True);p.add_argument('--model',type=Path,required=True)
    print(json.dumps(run(p.parse_args())))
