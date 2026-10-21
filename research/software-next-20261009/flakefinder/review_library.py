"""Small saved-run directory. No image decoding or instrument imports."""
from pathlib import Path
from urllib.parse import quote
import hashlib,json,re,threading,time


class ReviewLibrary:
    def __init__(self,root):
        self.root=Path(root).resolve();self._lock=threading.Lock();self._saved=None;self._until=0

    @staticmethod
    def _json(path):
        try:
            if path.stat().st_size>4*1024**2:return {}
            value=json.loads(path.read_text(encoding='utf-8-sig'))
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):return {}

    def data(self):
        with self._lock:
            if self._saved is not None and time.monotonic()<self._until:return self._saved
            runs=[];warnings=[]
            for run in sorted((self.root/'data').glob('scan-*'),reverse=True):
                if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',run.name):continue
                request=self._json(run/'scan-request.json');settings=request.get('settings',{})
                if request.get('id')!=run.name:continue
                workflow=self._json(run/'offline-workflow/status.json')
                if workflow and workflow.get('state') not in ('complete','complete_with_warnings'):
                    runs.append({'id':run.name+'-workflow','run_id':run.name,'title':settings.get('sample_id') or run.name,
                        'material':settings.get('material'),'operator_name':settings.get('operator_name',''),'kind':'workflow','curated':False,'state':workflow.get('state'),
                        'phase':workflow.get('phase'),'reason':workflow.get('reason'),'fields':None,
                        'version':'Automatic scan-to-review','href':'/processing?run='+quote(run.name),'refinement_selected':False})
                material=settings.get('material')
                if material not in ('graphene','hBN'):continue
                namespace='hbn' if material=='hBN' else 'graphene'
                selectors=[run/f'{namespace}-analysis-selection.json',*sorted(run.glob(namespace+'-analysis-selection-chip*.json'))]
                selectors=[p for p in selectors if p.is_file()];seen=set()
                for selector_path in selectors or [None]:
                    curated=selector_path is not None;chip=None;version='Incremental analysis'
                    if curated:
                        selector=self._json(selector_path);relative=Path(selector.get('analysis_relative_path',''))
                        source=(self.root/relative).resolve();expected=(self.root/'analysis'/namespace/run.name).resolve()
                        if relative.is_absolute() or '..' in relative.parts or not source.is_relative_to(expected):
                            warnings.append(run.name+': invalid selected analysis path');continue
                        try:
                            raw=(source/'plan.json').read_bytes()
                            if hashlib.sha256(raw).hexdigest()!=selector.get('plan_sha256'):raise ValueError('plan checksum')
                            plan=json.loads(raw);identity=plan['pin']['identity'];chip=source.parent.name
                            if identity['run_id']!=run.name or not re.fullmatch(r'chip\d+',chip):raise ValueError('plan identity')
                            if plan['pin'].get('chip',chip)!=chip:raise ValueError('chip identity')
                            if identity.get('scan_request_sha256') and identity['scan_request_sha256']!=hashlib.sha256((run/'scan-request.json').read_bytes()).hexdigest():raise ValueError('request checksum')
                            if selector_path.name!=namespace+'-analysis-selection.json' and selector_path.name!=f'{namespace}-analysis-selection-{chip}.json':raise ValueError('selector chip mismatch')
                        except (OSError,ValueError,KeyError):
                            warnings.append(run.name+': selected analysis unavailable');continue
                        if chip in seen:continue
                        seen.add(chip);version=source.name
                    else:source=run/('analysis-'+namespace+'-incremental')
                    status=self._json(source/'status.json');processed=status.get('finished_fields',status.get('processed_fields',0))
                    if not curated and not processed:continue
                    running_hbn=material=='hBN' and status.get('state') in ('starting','running','waiting_for_ram','resource_paused','paused_for_quiet_window','waiting_for_committed_tiles')
                    counts=status.get('counts',{});specific=selector_path is not None and selector_path.name!=namespace+'-analysis-selection.json'
                    runs.append({'id':run.name+('-'+chip if specific else ''),'run_id':run.name,'chip_id':chip,
                        'title':(settings.get('sample_id') or run.name)+(' - '+chip if chip else ''),
                        'material':'hBN' if material=='hBN' else 'Graphene','operator_name':settings.get('operator_name',''),'oxide_nm':settings.get('oxide_nm'),'kind':namespace,'curated':curated,
                        'state':status.get('state','saved'),'fields':processed,'assessed_fields':counts.get('assessed'),
                        'version':version,'href':'/graphene?run='+quote(run.name)+('&chip='+chip if specific else '')+('&filter=all&target_band=all' if running_hbn else ''),
                        'workflow_state':workflow.get('state'),
                        'refinement_selected':(run/f'{namespace}-review-selection-{chip}.json').is_file() or (not specific and (run/f'{namespace}-review-selection.json').is_file())})
            legacy=self.root/'data/whole-chip-standardized-01'
            status=self._json(legacy/'analysis-residue-sieves-campaign-01/status.json')
            if status:
                runs.append({'id':legacy.name,'title':'MoTe2 - whole-chip collection','material':'MoTe2',
                    'kind':'historical','curated':True,'state':status.get('state','saved'),
                    'fields':status.get('processed_fields',0),'assessed_fields':status.get('successful_fields'),
                    'version':'Residue-sieve campaign','href':'/library','refinement_selected':False})
            self._saved={'runs':runs,'warnings':warnings,'scope':'Explicit per-chip analyses, queued offline work and historical reviewed collections.'}
            self._until=time.monotonic()+30
            return self._saved
