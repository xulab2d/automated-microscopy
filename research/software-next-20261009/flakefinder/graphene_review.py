"""Read-only, run-pinned projection of committed graphene analysis shards.

The watcher's small top-ranked catalog is not an observation index. This reader
indexes every committed field, validates field JSON against its ledger digest,
and only returns one page of full geometries at a time. No instrument imports.
"""
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote
import hashlib
import json
import math
import re
import threading
import array
import base64
import sys
import sqlite3
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor


class GrapheneReview:
    # Keep compact observations for only the two most recently used analyses.
    # Disk projections and scientific source files are never evicted here.
    MAX_MEMORY_ANALYSES=2
    INDEX_SLICE_SECONDS=2.

    def __init__(self, project, *, cache_directory=None):
        self.root = Path(project).resolve()
        self._index = {}
        self._active_index = {}
        self._field_index_keys = {}
        self._shards = OrderedDict()
        self._snapshots = {}
        self._lock = threading.RLock()
        self._paths={}
        self._views=OrderedDict()
        self._display_rows=OrderedDict()
        self._full_rows=OrderedDict()
        self._refinement_shards=OrderedDict()
        self._pool=None
        self._cache_directory=Path(cache_directory) if cache_directory else Path(tempfile.gettempdir())/'flakefinder-graphene-review'
        self._disk=None
        self._manifest_fields={}
        self._color_cache=None
        self._annotation_paths={}
        self._match_radii={}
        self._score_version=None
        self._scores={}
        self._analysis_lru=OrderedDict()
        self._analysis_snapshot_keys={}
        self._hbn_screens={}
        self._human_feedback={}
        self._snapshot_owner=None
        from .review_feedback import ReviewFeedback
        self._feedback=ReviewFeedback(self.root)

    @staticmethod
    def _feedback_identity(run_id,request_hash,source,field,commit):
        tile=field.split('/')[1]
        return {'run_id':run_id,'observation':None,'material':source['material'],
            'scan_request_sha256':request_hash,'analysis_relative_path':source['analysis_relative_path'],
            'plan_sha256':source.get('plan_sha256'),
            'field_sha256':commit.get('hashes',{}).get(commit.get('field_json')),
            'source_tiff_sha256':commit.get('source_hashes',{}).get('tiles/'+tile+'.tiff')}

    def save_feedback(self,run_id,observation,identity,**values):
        """Validate only the selected source field before saving human evidence."""
        if not isinstance(observation,str) or not re.fullmatch(r'chip\d+/tile_\d+/\d+',observation):
            raise ValueError('Invalid observation ID')
        field=observation.rsplit('/',1)[0];chip=field.split('/')[0]
        with self._lock:
            run,request,request_hash=self._run(run_id)
            offline=self._offline(run,run_id,request_hash,chip)
            if offline:
                output,status,ledger,stale,source=offline
            else:
                namespace='hbn' if request['settings']['material']=='hBN' else 'graphene'
                output=run/('analysis-'+namespace+'-incremental')
                ledger,stale=self._read(output/'ledger.json',{},refresh=True)
                source={'material':request['settings']['material'],'analysis_relative_path':output.relative_to(self.root).as_posix()}
            if stale:raise ValueError('Current source is unavailable; reopen the region before labeling')
            commit=ledger.get(field)
            if not commit:raise ValueError('Observation is not committed in the selected analysis')
            expected={**self._feedback_identity(run_id,request_hash,source,field,commit),'observation':observation}
            if identity!=expected:
                from .review_feedback import FeedbackConflict
                raise FeedbackConflict('Source changed; reopen this region before labeling')
            payload=self._shard(output,field,commit)
            row=next((r for r in payload['rows'] if r['id']==observation),None)
            if row is None:raise ValueError('Observation is absent from its source field')
            region={k:row.get(k) for k in ('id','field','instance_label','polygons_px','bbox_px','source_shape_hw',
                'contrast_rgb','area_um2','stage_xy_um','source_tiff_sha256','source_transform_sha256')}
            return self._feedback.append(expected,source_region=region,**values)

    def _note_snapshot(self,key):
        if self._snapshot_owner is not None:
            self._analysis_snapshot_keys.setdefault(self._snapshot_owner,set()).add(key)

    def _touch_analysis(self,output):
        """Under _lock: release old in-memory namespaces; revisit revalidates disk."""
        namespace=str(output)
        self._analysis_lru[namespace]=None;self._analysis_lru.move_to_end(namespace)
        while len(self._analysis_lru)>self.MAX_MEMORY_ANALYSES:
            old,_=self._analysis_lru.popitem(last=False)
            self._hbn_screens.pop(old,None)
            self._human_feedback.pop(old,None)
            self._index={k:v for k,v in self._index.items() if k[0]!=old}
            self._active_index.pop(old,None);self._manifest_fields.pop(old,None)
            self._field_index_keys={k:v for k,v in self._field_index_keys.items() if k[0]!=old}
            self._paths={k:v for k,v in self._paths.items() if k[0]!=old}
            for cache in (self._display_rows,self._full_rows):
                for key in list(cache):
                    if key[0][0]==old:del cache[key]
            for key in list(self._shards):
                if Path(key[0]).is_relative_to(Path(old)):del self._shards[key]
            released=self._analysis_snapshot_keys.pop(old,set())
            retained=set().union(*self._analysis_snapshot_keys.values()) if self._analysis_snapshot_keys else set()
            for key in released-retained:self._snapshots.pop(key,None)
            released_paths={key[1] for key in released-retained if isinstance(key,tuple)}
            self._annotation_paths={k:v for k,v in self._annotation_paths.items() if v not in released_paths}
            # These bounded projections can reference old compact rows or sibling
            # refinement folders; clearing is conservative and source-independent.
            self._views.clear();self._refinement_shards.clear()
            self._scores.clear();self._score_version=None;self._color_cache=None

    def _disk_cache(self):
        if self._disk is None:
            self._cache_directory.mkdir(parents=True,exist_ok=True)
            name=hashlib.sha256(str(self.root).encode()).hexdigest()[:24]+'-v4.sqlite'
            self._disk=sqlite3.connect(self._cache_directory/name,check_same_thread=False,timeout=10)
            self._disk.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA cache_size=-4096;
              CREATE TABLE IF NOT EXISTS fields(key TEXT PRIMARY KEY, namespace TEXT UNIQUE, body BLOB, sha TEXT);
              CREATE TABLE IF NOT EXISTS displays(key TEXT, id TEXT, body BLOB, sha TEXT, PRIMARY KEY(key,id));''')
        return self._disk

    @staticmethod
    def _encode_cache(value):
        raw=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
        return zlib.compress(raw,1),hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _decode_cache(record):
        if record is None:return None
        raw=zlib.decompress(record[0])
        if hashlib.sha256(raw).hexdigest()!=record[1]:raise ValueError('Disposable review cache checksum mismatch')
        return json.loads(raw)

    def _cached_field(self,key):
        try:return self._decode_cache(self._disk_cache().execute('SELECT body,sha FROM fields WHERE key=?',(key,)).fetchone())
        except (sqlite3.Error,OSError,ValueError,zlib.error):return None

    def _cached_area_field(self,index_key,namespace):
        """Reuse validated all-region projections without reloading scientific shards."""
        key=self._canonical(index_key)
        cached=self._cached_field(key)
        if cached is not None:return cached
        for old_key in (self._canonical(index_key[:-1]),self._canonical((*index_key[:-1],0.))):
            if old_key==key:continue
            old=self._cached_field(old_key)
            if old is None:continue
            compact,summary=old
            compact=[r for r in compact if isinstance(r.get('area_um2'),(int,float)) and r['area_um2']>=index_key[-1]]
            try:
                db=self._disk_cache();body,sha=self._encode_cache([compact,summary])
                with db:
                    previous=db.execute('SELECT key FROM fields WHERE namespace=?',(namespace,)).fetchone()
                    if previous:
                        db.execute('DELETE FROM displays WHERE key=?',(previous[0],))
                        db.execute('DELETE FROM fields WHERE key=?',(previous[0],))
                    db.executemany('INSERT OR REPLACE INTO displays SELECT ?,id,body,sha FROM displays WHERE key=? AND id=?',
                        ((key,old_key,row['id']) for row in compact))
                    db.execute('INSERT INTO fields VALUES(?,?,?,?)',(key,namespace,body,sha))
            except (sqlite3.Error,OSError):pass
            return compact,summary
        return None

    @staticmethod
    def _display(row):
        keys=('id','field','instance_label','disposition','area_um2','rank','contrast_rgb','bbox_px','source_shape_hw',
            'preview','filter_reasons','stage_xy_um','source_tiff_sha256','source_transform_sha256',
            'usable_width_um','class_name','proposal_stage','optical_evidence')
        return {k:row[k] for k in keys if k in row}

    def _optical_evidence(self,row,classifier):
        population=next((s for s in row.get('sieve_results',[]) if s.get('name')=='material_color_population'),{})
        statistics=classifier.get('statistics',{})
        def finite(value,minimum=None,maximum=None):
            return float(value) if isinstance(value,(int,float)) and math.isfinite(value) and (minimum is None or value>=minimum) and (maximum is None or value<=maximum) else None
        quantile=finite(statistics.get('match_quantile'),0.,1.)
        radius=None
        if quantile is not None and 0<quantile<1:
            if quantile not in self._match_radii:
                from scipy.stats import chi2
                self._match_radii[quantile]=float(chi2.ppf(quantile,3))
            radius=self._match_radii[quantile]
        empirical={}
        if isinstance(classifier.get('empirical_trajectory'),dict):
            # Use saved decisions, not a new RGB projection with missing covariance.
            # Perpendicular distance alone says nothing about amplitude support.
            name=row.get('class_name')
            if population.get('status')=='compatible':tier,scope=0,'observed_band_supported'
            elif name=='extrapolated_rgb_trajectory_unconfirmed':tier,scope=1,'trajectory_extrapolated_unconfirmed'
            elif name=='operator_negative_achromatic_population':tier,scope=3,'operator_negative_population'
            else:tier,scope=2,'unclassified_optical_feature'
            empirical={'empirical_support_tier':tier,'empirical_support_scope':scope,
                'empirical_support_provenance':'saved_field_classifier.empirical_trajectory and saved region class/material sieve',
                'supported_red_interval':classifier['empirical_trajectory'].get('supported_red_interval'),
                'support_qualification':'Review ordering only; no layer or chemical assignment. All observations remain accessible.'}
        return {'reference_distance':finite(population.get('reference_distance'),0.),
            'support_fraction':finite(population.get('support_fraction',row.get('material_pixel_fraction')),0.,1.),
            'log_likelihood_ratio':finite(population.get('log_likelihood_ratio_vs_substrate')),
            'match_radius_squared':radius,'match_quantile':quantile,
            'minimum_log_likelihood_ratio':finite(statistics.get('minimum_log_likelihood_ratio')),
            'interior_measurement_available':row.get('measurement',{}).get('interior_measurement_available'),
            'threshold_provenance':'saved_field_classifier.statistics' if radius is not None else 'unavailable',**empirical}

    @staticmethod
    def _review_tier(row):
        return row.get('optical_evidence',{}).get('empirical_support_tier',0)

    @staticmethod
    def _joint_factors(row,refinement):
        evidence=row.get('optical_evidence',{});distance=evidence.get('reference_distance');radius2=evidence.get('match_radius_squared')
        compatibility=.25 if distance is None or radius2 is None or radius2<=0 else (1./math.hypot(1.,distance/math.sqrt(radius2)))**2
        support=evidence.get('support_fraction');support_factor=.5+.5*(support if support is not None else 0.)
        llr=evidence.get('log_likelihood_ratio');minimum=evidence.get('minimum_log_likelihood_ratio')
        substrate=.5 if llr is None or minimum is None else 1./(1.+max(0.,minimum-llr))
        area=max(0.,float(row.get('area_um2') or 0.));area_utility=math.log1p(min(area,1000.)/25.)/math.log1p(1000./25.)
        quality=None if refinement.get('refinement_status')=='unresolved' else refinement.get('rank')
        geometry=.5*area_utility if not isinstance(quality,(int,float)) or not math.isfinite(quality) else min(1.,max(0.,float(quality)/math.log1p(1000./25.)))
        return compatibility,support_factor,substrate,geometry,area_utility

    @staticmethod
    def _joint_score(row,refinement):
        """Versioned heuristic priority; factors are not material probabilities."""
        compatibility,support,substrate,geometry,area=GrapheneReview._joint_factors(row,refinement)
        evidence=row.get('optical_evidence',{});missing=[]
        if evidence.get('reference_distance') is None or not evidence.get('match_radius_squared'):missing.append('reference_distance_or_model_threshold')
        if evidence.get('support_fraction') is None:missing.append('material_support')
        if evidence.get('log_likelihood_ratio') is None or evidence.get('minimum_log_likelihood_ratio') is None:missing.append('substrate_evidence_or_model_threshold')
        quality=None if refinement.get('refinement_status')=='unresolved' else refinement.get('rank')
        if not isinstance(quality,(int,float)) or not math.isfinite(quality):missing.append('boundary_geometry')
        return {'version':'empirical_support_then_joint_v1' if 'empirical_support_tier' in evidence else 'joint_evidence_v1','optical_compatibility':compatibility,'material_support':support,
            'substrate_evidence':substrate,'geometry_quality':geometry,'area_utility':area,
            'boundary_quality_factor':geometry/area if area else 0.,
            'boundary_evidence_status':refinement.get('refinement_status','pending'),
            'boundary_evidence_flags':refinement.get('artifact_flags',[]),
            'area_already_in_geometry':True,'combined_score':geometry*compatibility*support*substrate,
            'evidence_missing':missing,**evidence,
            'qualification':'Heuristic review priority, not material probability. No observation is excluded by this score.'}

    def _store_field(self,key,namespace,compact,summary,rows):
        """Disposable local projections; source bytes and directories are never written."""
        try:
            db=self._disk_cache();body,sha=self._encode_cache([compact,summary])
            with db:
                old=db.execute('SELECT key FROM fields WHERE namespace=?',(namespace,)).fetchone()
                if old:
                    db.execute('DELETE FROM displays WHERE key=?',(old[0],));db.execute('DELETE FROM fields WHERE key=?',(old[0],))
                db.executemany('INSERT INTO displays VALUES(?,?,?,?)',
                    ((key,row['id'],*self._encode_cache(self._display(row))) for row in rows))
                db.execute('INSERT INTO fields VALUES(?,?,?,?)',(key,namespace,body,sha))
        except (sqlite3.Error,OSError):pass  # Optional cache loss cannot change source validation.

    def _page_row(self,output,field,commit,index_key,identifier,detail):
        cache=self._full_rows if detail else self._display_rows
        key=(index_key,identifier)
        if key in cache:
            cache.move_to_end(key);return cache[key]
        row=None
        if not detail:
            try:row=self._decode_cache(self._disk_cache().execute('SELECT body,sha FROM displays WHERE key=? AND id=?',
                (self._canonical(index_key),identifier)).fetchone())
            except (sqlite3.Error,OSError,ValueError,zlib.error):pass
        if row is None:
            payload=self._shard(output,field,commit)
            row=next(r for r in payload['rows'] if r['id']==identifier)
            if not detail:row=self._display(row)
        cache[key]=row
        while len(cache)>192:cache.popitem(last=False)
        return row

    def _field_stats(self,output,ledger):
        def stat(item):
            field,commit=item
            try:
                token=(str(output),commit['field_json'])
                path=self._paths.get(token)
                if path is None:path=self._inside(output,commit['field_json'])
                value=path.stat()
                return field,token,path,(value.st_mtime_ns,value.st_size,value.st_ino,value.st_dev)
            except (OSError,ValueError,KeyError,TypeError) as exc:return field,None,None,exc
        specs=list(ledger.items())
        if len(specs)<64:results=map(stat,specs)
        else:
            if self._pool is None:self._pool=ThreadPoolExecutor(max_workers=8,thread_name_prefix='graphene-metadata')
            results=self._pool.map(stat,specs)
        signatures={}
        for field,token,path,signature in results:
            if path is not None:self._paths[token]=path
            signatures[field]=signature
        return signatures

    @staticmethod
    def _inside(root, relative):
        value = Path(relative)
        if value.is_absolute() or value.drive or '..' in value.parts:
            raise ValueError('Analysis path must stay inside this run')
        path = (root/value).resolve()
        if not path.is_relative_to(root): raise ValueError('Analysis path leaves this run')
        return path

    def _read(self, path, default=None, limit=32*1024**2, *, refresh=False):
        self._note_snapshot(str(path))
        try:
            stat = path.stat(); signature=(stat.st_mtime_ns,stat.st_size)
            previous=self._snapshots.get(str(path))
            if previous and previous[0]==signature and not refresh: return previous[1],False
            if stat.st_size > limit: raise ValueError('Saved analysis record is too large')
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(value,dict): raise ValueError('Expected a saved analysis object')
            if path.name=='manifest.json':
                completed=value.get('completed',{})
                value={'completed':dict.fromkeys(completed,{}) if isinstance(completed,dict) else {}}
            self._snapshots[str(path)] = (signature,value)
            return value, False
        except (OSError, ValueError, UnicodeError):
            if str(path) in self._snapshots: return self._snapshots[str(path)][1], True
            return default, path.exists()

    def _run(self, run_id):
        if not isinstance(run_id,str) or not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',run_id):
            raise ValueError('Choose one exact scan run')
        folder = self._inside(self.root,'data/'+run_id)
        path = folder/'scan-request.json'
        raw = path.read_bytes()
        request = json.loads(raw)
        if request.get('id') != run_id or request.get('settings',{}).get('material') not in ('graphene','hBN'):
            raise ValueError('This page only reviews an exact selected graphene or hBN scan')
        return folder, request, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _canonical(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

    def _pinned(self, path, expected, *, canonical=False, project=None):
        """No stale fallback for immutable evidence; cache only a compact projection."""
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
            raise ValueError('Missing offline evidence checksum')
        stat=path.stat();signature=(stat.st_mtime_ns,stat.st_size,expected,canonical)
        key=('pinned',str(path));self._note_snapshot(key);previous=self._snapshots.get(key)
        if previous and previous[0]==signature:return previous[1]
        if stat.st_size>32*1024**2:raise ValueError('Offline evidence record too large')
        raw=path.read_bytes();value=json.loads(raw)
        actual=self._canonical(value) if canonical else hashlib.sha256(raw).hexdigest()
        if actual!=expected:raise ValueError('Offline evidence checksum mismatch: '+path.name)
        if not isinstance(value,dict):raise ValueError('Expected offline evidence object')
        value=project(value) if project else value
        self._snapshots[key]=(signature,value)
        return value

    def _offline(self, run, run_id, request_hash, chip=''):
        _,request,current_hash=self._run(run_id)
        if current_hash!=request_hash:raise ValueError('Scan request changed while selecting analysis')
        material=request['settings']['material'];namespace='hbn' if material=='hBN' else 'graphene'
        oxide=request['settings'].get('oxide_nm')
        if chip and not re.fullmatch(r'chip\d+',chip):raise ValueError('Invalid selected chip')
        specific=run/f'{namespace}-analysis-selection-{chip}.json' if chip else None
        selector_path=specific if specific is not None and specific.exists() else run/f'{namespace}-analysis-selection.json'
        if not selector_path.exists():return None
        selector=json.loads(selector_path.read_bytes())
        if selector.get('schema')!=1 or selector.get('run_id')!=run_id:
            raise ValueError('Offline analysis selection differs from selected run')
        relative=selector.get('analysis_relative_path')
        if not isinstance(relative,str):raise ValueError('Missing selected offline analysis path')
        output=self._inside(self.root,relative)
        parts=output.relative_to(self.root).parts
        if len(parts)!=5 or parts[:3]!=('analysis',namespace,run_id) or not re.fullmatch(r'chip\d+',parts[3]) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',parts[4]):
            raise ValueError('Offline analysis path must name this exact run, chip and version')
        self._snapshot_owner=str(output)
        plan=self._pinned(output/'plan.json',selector.get('plan_sha256'))
        pin=plan['pin'];identity=pin['identity']
        if chip and chip!=parts[3]:raise ValueError('No selected offline analysis for requested chip')
        chip=parts[3]
        if identity.get('run_id')!=run_id or identity.get('scan_request_sha256')!=request_hash or pin.get('chip')!=chip:
            raise ValueError('Offline analysis is not pinned to this scan request/chip')
        if self._canonical(pin['config'])!=pin.get('config_sha256'):
            raise ValueError('Offline analysis config checksum mismatch')
        model=self._pinned(output/'model.json',plan.get('model_sha256'),canonical=True)
        context=model.get('applicability_context',model)
        if context.get('material')!=material or context.get('oxide_nm')!=oxide:
            raise ValueError('Offline model differs from selected material/substrate context')
        if material=='hBN' and model.get('empirical_trajectory'):
            raise ValueError('Graphene empirical trajectory cannot be applied to hBN')
        def compact_manifest(value):
            records={}
            for tile,row in value['completed'].items():
                required=(f'tiles/{tile}.tiff',f'tiles/{tile}.json',f'boundary-labels/{tile}.npz')
                records[tile]={'source_hashes':{p:row.get('hashes',{}).get(p) for p in required},
                    'source_record_sha256':self._canonical(row)}
            return records
        records=self._pinned(run/chip/'manifest.json',pin.get('manifest_sha256'),project=compact_manifest)
        self._manifest_fields[str(output)]=[chip+'/'+tile for tile in records]
        tiles=pin['config']['tiles']
        if not isinstance(tiles,list) or len(set(tiles))!=len(tiles) or any(t not in records or not re.fullmatch(r'tile_\d+',t) for t in tiles):
            raise ValueError('Offline selected fields differ from pinned manifest')
        ledger,stale=self._read(output/'ledger.json',{})
        commits={}
        tile_set=set(tiles)
        for tile,commit in ledger.items():
            if tile not in tile_set or not isinstance(commit,dict):raise ValueError('Offline ledger field outside selected manifest')
            commits[chip+'/'+tile]={**commit,'_offline':True,**records[tile],
                '_model_sha256':plan['model_sha256'],'_factor':pin['config']['factor']}
        saved,status_stale=self._read(output/'status.json',{})
        states=('assessed','unassessable','budget_exhausted','invalid_source','failed')
        if any(c.get('state') not in states for c in commits.values()):raise ValueError('Invalid offline field assessment state')
        counts={name:sum(c.get('state')==name for c in commits.values()) for name in states}
        status={**saved,'run_id':run_id,'scan_request_sha256':request_hash,'processed_fields':len(commits),
            'finished_fields':len(commits),'assessed_fields':counts['assessed'],'unassessable_fields':counts['unassessable'],
            'failed_fields':counts['failed']+counts['invalid_source'],'budget_exhausted_fields':counts['budget_exhausted'],
            'available_fields':len(tiles),'total_fields':len(tiles),'counts':counts}
        if status.get('state')=='complete' and len(commits)!=len(tiles):raise ValueError('Completed offline status has an incomplete ledger')
        source={'material':material,'material_label':material if material=='hBN' else 'Graphene','namespace':namespace,'kind':'offline_selected','analysis_relative_path':relative,'plan_sha256':selector['plan_sha256'],
            'model_sha256':plan['model_sha256'],'config_sha256':pin['config_sha256'],'chip':chip,'version':parts[4],'selection_file':selector_path.name,'empirical_trajectory':model.get('empirical_trajectory')}
        return output,status,commits,bool(stale or status_stale),source

    def _related(self, request):
        contract=request.get('rectangle_handoff')
        if not isinstance(contract,dict):return None
        prior=contract.get('prior_run_id');expected=contract.get('prior_files_sha256',{}).get('scan-request.json')
        if not prior or not expected:return None
        try:
            run,_,request_hash=self._run(prior)
            if request_hash!=expected:raise ValueError('Related donor request checksum mismatch')
            offline=self._offline(run,prior,request_hash)
            if offline:return {'run_id':prior,'url':'/graphene?run='+quote(prior),'analysis_source':offline[4],
                'relationship':'Explicit acquisition handoff donor; this page has not redirected its data.'}
        except (OSError,ValueError,KeyError,TypeError):return None
        return None

    def _offline_rows(self, value, field, commit):
        if value.get('source_hashes')!=commit['source_hashes'] or value.get('assessment_state')!=commit.get('state'):
            raise ValueError('Offline field source/state differs from pinned ledger/manifest')
        assessed=value.get('assessment_state')=='assessed';regions=value.get('regions')
        if not assessed:
            if regions not in (None,[]):raise ValueError('Unassessable field contains proposed regions')
            return []
        if not isinstance(regions,list) or value.get('source_record_sha256')!=commit['source_record_sha256']:
            raise ValueError('Offline field acquisition record mismatch')
        if value.get('classifier',{}).get('model_sha256')!=commit['_model_sha256'] or value.get('reduction',{}).get('factor')!=commit['_factor']:
            raise ValueError('Offline field model/reduction differs from plan')
        rows=[];seen=set();chip,tile=field.split('/')
        for region in regions:
            label=region.get('instance_label');area=region.get('area_um2');polygon=region.get('polygon_source_px');holes=region.get('holes_source_px',[])
            if type(label) is not int or label<=0 or label in seen or not isinstance(area,(int,float)) or not math.isfinite(area) or area<0:
                raise ValueError('Invalid offline region identity/area')
            seen.add(label)
            shape=region.get('source_shape_hw')
            if not isinstance(shape,list) or len(shape)!=2 or any(type(n) is not int or n<=0 for n in shape):raise ValueError('Invalid source image shape')
            if not isinstance(polygon,list) or not polygon or not isinstance(holes,list):raise ValueError('Missing source polygon')
            for ring in [polygon,*holes]:
                if not isinstance(ring,list) or any(not isinstance(p,list) or len(p)!=2 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in p) or not (0<=p[0]<shape[1] and 0<=p[1]<shape[0]) for p in ring):
                    raise ValueError('Invalid source polygon coordinates')
            useful=any(s.get('name')=='useful_size' and s.get('status')=='pass' for s in region.get('sieve_results',[]))
            selected=region.get('selected_for_review') is True and useful
            xs=[p[0] for p in polygon];ys=[p[1] for p in polygon]
            fraction=region.get('material_pixel_fraction',0.)
            if not isinstance(fraction,(int,float)) or not math.isfinite(fraction) or not 0<=fraction<=1:raise ValueError('Invalid material fraction')
            rows.append({**region,'id':field+'/'+str(label),'field':field,'disposition':'shortlist' if selected else 'uncertain',
                'rank':float(area)*fraction,'analysis_polygon_px':region.get('polygon_px'),'polygon_px':polygon,
                'holes_px':holes,'polygons_px':[polygon,*holes],'bbox_px':[min(xs),min(ys),max(xs)-min(xs)+1,max(ys)-min(ys)+1],
                'preview':chip+'/tiles/'+tile+'.jpg','source_tiff_sha256':commit['source_hashes']['tiles/'+tile+'.tiff'],
                'source_transform_sha256':self._canonical(value['source_transform']),
                'layer_number':None,'chemical_identity_confirmed':False})
        return rows

    def _shard(self, output, field, commit):
        relative = commit.get('field_json')
        if not isinstance(relative,str): raise ValueError('Missing committed field path')
        expected = commit.get('hashes',{}).get(relative)
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):
            raise ValueError('Missing committed field checksum')
        path = self._inside(output,relative)
        stat = path.stat(); key = (str(path),expected,stat.st_mtime_ns,stat.st_size,
            self._canonical(commit) if commit.get('_offline') else None)
        if key in self._shards:
            self._shards.move_to_end(key)
            return self._shards[key]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected: raise ValueError('Committed field checksum mismatch')
        value = json.loads(raw)
        if commit.get('_offline'):
            if value.get('field')!=field:raise ValueError('Committed field identity differs from ledger')
            value={**value,'rows':self._offline_rows(value,field,commit)}
            value.pop('regions',None)
        if value.get('field') != field or not isinstance(value.get('rows'),list):
            raise ValueError('Committed field identity differs from ledger')
        for row in value['rows']:
            if not isinstance(row,dict) or row.get('field') != field or not str(row.get('id','')).startswith(field+'/'):
                raise ValueError('Region identity differs from source field')
            row['optical_evidence']=self._optical_evidence(row,value.get('classifier',{}))
        self._shards[key] = value
        # Dense hBN fields legitimately exceed 32 MB. Keep them readable while
        # bounding retained shards by source bytes as well as count. One current
        # field must fit; this is cache eviction, never a scientific exclusion.
        while len(self._shards)>1 and (len(self._shards)>12 or sum(k[3] for k in self._shards)>32*1024**2):
            self._shards.popitem(last=False)
        return value

    @staticmethod
    def _filtered(row):
        if 'filtered' in row:return row['filtered']
        return bool(row.get('filter_reasons')) or any(s.get('status') in ('fail','rejected','below_priority') for s in row.get('sieve_results',[]))

    def _review_annotations(self, run, run_id, source, ledger):
        self._annotation_stale=False
        namespace=source.get('namespace','graphene')
        specific=run/(namespace+'-review-selection-'+source.get('chip','')+'.json')
        selector_path=specific if specific.is_file() else run/(namespace+'-review-selection.json')
        if not selector_path.is_file():return {},{}
        selector=json.loads(selector_path.read_bytes())
        if selector_path.name==namespace+'-review-selection.json' and source.get('selection_file',namespace+'-analysis-selection.json')!=namespace+'-analysis-selection.json' and selector.get('plan_sha256')!=source.get('plan_sha256'):
            return {},{}
        if selector.get('schema')!=1 or selector.get('run_id')!=run_id or selector.get('plan_sha256')!=source.get('plan_sha256'):
            raise ValueError('Review derivative differs from selected analysis')
        results={}
        base=self._inside(self.root,source['analysis_relative_path']).parent
        for name in ('refinement','dedup'):
            item=selector.get(name)
            if not item:continue
            path=self._inside(self.root,item['path']);refinement_plan=None
            if not path.is_relative_to(base):raise ValueError('Review derivative outside selected chip analysis')
            if item.get('kind')=='stream':
                if name!='refinement':raise ValueError('Only refinement supports a generation stream')
                directory=path;refinement_plan=item.get('plan_sha256')
                plan=self._pinned(directory/'plan.json',refinement_plan)
                if plan.get('run_id')!=run_id or plan.get('plan_sha256',plan.get('source_plan_sha256'))!=source['plan_sha256']:
                    raise ValueError('Refinement plan differs from selected analysis')
                latest,stale=self._read(directory/'latest.json',{},refresh=True)
                self._annotation_stale=bool(stale)
                if latest.get('refinement_plan_sha256')!=refinement_plan:raise ValueError('Refinement generation plan mismatch')
                path=self._inside(directory,latest['index_path']);expected=latest['sha256']
            else:directory=path.parent;expected=item['sha256']
            if path.suffix!='.json':raise ValueError('Invalid review derivative type')
            value=self._pinned(path,expected)
            if value.get('schema') not in (1,2) or value.get('run_id')!=run_id or value.get('plan_sha256')!=source['plan_sha256']:
                raise ValueError('Review evidence identity mismatch')
            if refinement_plan and value.get('refinement_plan_sha256')!=refinement_plan:raise ValueError('Refinement index plan mismatch')
            sources=value.get('source_fields')
            if not isinstance(sources,dict):raise ValueError('Review source fields missing')
            for field,digest in sources.items():
                commit=ledger.get(field,{})
                if digest!=commit.get('hashes',{}).get(commit.get('field_json')):raise ValueError('Review source field checksum mismatch')
            previous=self._annotation_paths.get((run_id,name))
            if previous and previous!=str(path):self._snapshots.pop(('pinned',previous),None)
            self._annotation_paths[run_id,name]=str(path)
            results[name]={**value,'_directory':str(directory),'_version':expected}
        return results.get('refinement',{}),results.get('dedup',{})

    def _refinement(self,index,identifier,detail):
        entry=index.get('regions',{}).get(identifier)
        if entry is None:return None
        if not detail:
            keys=('rank','likely_artifact','refinement_status','artifact_flags','useful_size','hbn_screen')
            return {k:entry[k] for k in keys if k in entry}
        if index.get('schema')!=2:return entry
        path=self._inside(Path(index['_directory']),entry['shard_path']);expected=entry['shard_sha256']
        stat=path.stat();key=(str(path),expected,stat.st_mtime_ns,stat.st_size)
        if key in self._refinement_shards:
            self._refinement_shards.move_to_end(key);value=self._refinement_shards[key]
        else:
            value=self._pinned(path,expected)
            self._snapshots.pop(('pinned',str(path)),None)
            field=identifier.rsplit('/',1)[0]
            if (value.get('run_id')!=index['run_id'] or value.get('plan_sha256')!=index['plan_sha256']
                or value.get('refinement_plan_sha256')!=index.get('refinement_plan_sha256')
                or value.get('field')!=field or value.get('source_field_sha256')!=index['source_fields'].get(field)):
                raise ValueError('Refinement shard identity mismatch')
            self._refinement_shards[key]=value
            while len(self._refinement_shards)>6:self._refinement_shards.popitem(last=False)
        return value['regions'][identifier]

    @staticmethod
    def _hbn_evidence(row,refinements):
        screen=refinements.get(row['id'],{}).get('hbn_screen')
        return screen if isinstance(screen,dict) and screen.get('policy_version')=='hbn-thin-film-evidence-v2' else {}

    @staticmethod
    def _hbn_halo_hidden(row,refinements):
        if GrapheneReview._human_positive(row):return False
        screen=GrapheneReview._hbn_evidence(row,refinements)
        return (screen.get('halo_screen',{}).get('likely_halo') is True
                and screen.get('operator_supported') is not True)

    @staticmethod
    def _hbn_disposition(row,refinements):
        if GrapheneReview._human_positive(row):return 'shortlist'
        if GrapheneReview._human_negative(row):return 'uncertain'
        screen=GrapheneReview._hbn_evidence(row,refinements)
        return 'shortlist' if (screen.get('operator_supported') is True or
            (screen.get('thin_supported') is True and screen.get('contamination_ambiguous') is False
             and not GrapheneReview._hbn_halo_hidden(row,refinements))) else 'uncertain'

    @staticmethod
    def _hbn_band_matches(row,refinements,target_band):
        if target_band=='all':return True
        if GrapheneReview._human_positive(row) and target_band=='both':return True
        screen=GrapheneReview._hbn_evidence(row,refinements)
        band=screen.get('target_band','unknown')
        # Visual examples remain retrievable despite a model mismatch, without
        # relabeling the physics or assigning either narrow thickness range.
        if screen.get('operator_supported') is True and target_band=='both':return True
        allowed={'both':('below_5_nm','5_to_30_nm','spans_target_bands'),
            'below_5_nm':('below_5_nm','spans_target_bands'),'5_to_30_nm':('5_to_30_nm','spans_target_bands'),
            'other':('thick_or_bulk','unknown')}
        return band in allowed[target_band]

    @staticmethod
    def _hbn_priority(row,refinements):
        if GrapheneReview._human_positive(row):return (-1,0.,0)
        screen=GrapheneReview._hbn_evidence(row,refinements)
        metrics=screen.get('metrics',{})
        evidence=metrics.get('target_log_evidence',min(metrics.get('target_vs_bulk_log_ratio',0.),metrics.get('target_vs_substrate_log_ratio',0.)))
        if not isinstance(evidence,(int,float)) or not math.isfinite(evidence):evidence=0.
        band={'below_5_nm':0,'spans_target_bands':1,'5_to_30_nm':2,'unknown':3,'thick_or_bulk':4}.get(screen.get('target_band'),3)
        return (GrapheneReview._hbn_disposition(row,refinements)!='shortlist',-evidence,band)

    @staticmethod
    def _human_positive(row):
        return (row.get('human_feedback') or {}).get('label') in ('thin_hbn','encapsulation_hbn','good_flake')

    @staticmethod
    def _human_negative(row):
        return (row.get('human_feedback') or {}).get('label') in ('bulk','not_flake')

    @staticmethod
    def _hbn_screen_reasons(row,refinements):
        screen=GrapheneReview._hbn_evidence(row,refinements)
        if not screen:return ['thin_film_evidence_pending']
        reasons=[r for r in screen.get('reasons',[]) if isinstance(r,str)]
        if screen.get('contamination_ambiguous') is True:reasons.append('thin_or_residue_ambiguous')
        if screen.get('operator_supported') is True:reasons.append('operator_visual_example_not_measured_thickness')
        return list(dict.fromkeys(reasons))

    def data(self, run_id, *, category='shortlist', chip='', field='', offset=0, limit=48, minimum_area=25., group_duplicates=True, hide_artifacts=True, observation='', include_fields=True, detail=True,target_band='both',progressive=False):
        if target_band not in ('all','both','below_5_nm','5_to_30_nm','other'):raise ValueError('Invalid conditional thickness range')
        if category not in ('shortlist','uncertain','rejected','all'): raise ValueError('Unknown proposal view')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=96:
            raise ValueError('Invalid page bounds')
        if not isinstance(minimum_area,(int,float)) or not math.isfinite(minimum_area) or minimum_area<0:
            raise ValueError('Minimum area must be nonnegative')
        if chip and not re.fullmatch(r'[A-Za-z0-9_-]+',chip): raise ValueError('Invalid chip key')
        if field and not re.fullmatch(r'[A-Za-z0-9_-]+/tile_\d+',field): raise ValueError('Invalid field key')
        if observation and not re.fullmatch(r'[A-Za-z0-9_-]+/tile_\d+/\d+',observation):raise ValueError('Invalid observation ID')
        with self._lock:
            self._snapshot_owner=None
            run,request,request_hash = self._run(run_id)
            acquisition,_ = self._read(run/'scan-status.json',{})
            offline=self._offline(run,run_id,request_hash,chip or (field.split('/')[0] if field else ''))
            if offline:
                output,status,ledger,stale,analysis_source=offline;ledger_stale=False
            else:
                namespace='hbn' if request['settings']['material']=='hBN' else 'graphene'
                output = self._inside(run,'analysis-'+namespace+'-incremental')
                self._snapshot_owner=str(output)
                status,stale = self._read(output/'status.json',{})
                ledger,ledger_stale = self._read(output/'ledger.json',{})
                analysis_source={'material':request['settings']['material'],'namespace':namespace,'kind':'incremental','analysis_relative_path':output.relative_to(self.root).as_posix()}
            if status and (status.get('run_id') != run_id or status.get('scan_request_sha256') != request_hash):
                raise ValueError('Analysis status is not pinned to this scan request')
            self._touch_analysis(output)
            refinement,dedup=self._review_annotations(run,run_id,analysis_source,ledger)
            refinements=refinement.get('regions',{})
            is_hbn=request['settings']['material']=='hBN'
            if is_hbn:self._hbn_screens[str(output)]=refinements
            groups={member:group for group in dedup.get('groups',[]) for member in group['observation_ids']}
            rows=[];fields=[];warnings=[];commits={};active_index=[]
            signatures=self._field_stats(output,ledger)
            index_area=0. if observation else float(minimum_area)
            deadline=time.monotonic()+self.INDEX_SLICE_SECONDS
            loads=0;waiting=0
            preferred=observation.rsplit('/',1)[0] if observation else field
            items=list(ledger.items())
            if preferred:items.sort(key=lambda item:item[0]!=preferred)
            for key,commit in items:
                if not re.fullmatch(r'[A-Za-z0-9_-]+/tile_\d+',key) or not isinstance(commit,dict):
                    warnings.append('Invalid ledger entry omitted');continue
                digest=commit.get('hashes',{}).get(commit.get('field_json'))
                try:
                    signature=signatures[key]
                    if isinstance(signature,Exception):raise signature
                    index_key=(str(output),key,digest,*signature,self._canonical(commit),index_area)
                    if index_key not in self._index:
                        if progressive and loads and time.monotonic()>=deadline:
                            waiting+=1
                            fields.append(dict(field=key,state='index_pending',reason='Saved results loading',region_count=None))
                            continue
                        loads+=1
                        disk_key=self._canonical(index_key)
                        namespace=str(output)+'/'+key+'/area-'+str(index_area)
                        cached=self._cached_area_field(index_key,namespace)
                        if cached is None:
                            payload=self._shard(output,key,commit)
                            eligible=[row for row in payload['rows'] if isinstance(row.get('area_um2'),(int,float)) and row['area_um2']>=index_area]
                            compact=[{**{k:row.get(k) for k in ('id','field','disposition','area_um2','rank','contrast_rgb','optical_evidence')},'filtered':self._filtered(row)} for row in eligible]
                            state=payload.get('assessment_state') or 'assessed'
                            summary=dict(field=key,state=state,reason=payload.get('reason'),region_count=len(payload['rows']))
                            self._store_field(disk_key,str(output)+'/'+key+'/area-'+str(index_area),compact,summary,eligible)
                        else:compact,summary=cached
                        old_key=self._field_index_keys.get((str(output),key))
                        if old_key is not None:self._index.pop(old_key,None)
                        self._index[index_key]=(compact,summary)
                        self._field_index_keys[str(output),key]=index_key
                    compact,summary=self._index[index_key]
                    rows.extend(compact);fields.append(summary);commits[key]=commit;active_index.append(index_key)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    fields.append(dict(field=key,state='record_unavailable',reason=str(exc),region_count=None))
                    warnings.append(key+': '+str(exc))
            # Add every manifest-committed source field, including fields that
            # have not yet reached the analysis worker or were unassessable.
            known={item['field'] for item in fields}
            if offline:source_fields=self._manifest_fields[str(output)]
            else:
                source_fields=[]
                for manifest_path in sorted(run.glob('chip*/manifest.json')):
                    if not manifest_path.resolve().is_relative_to(run):continue
                    manifest,_=self._read(manifest_path,{})
                    source_fields.extend(manifest_path.parent.name+'/'+tile for tile in manifest.get('completed',{}) if re.fullmatch(r'tile_\d+',tile))
            for key in source_fields:
                if key not in known:fields.append(dict(field=key,state='pending',reason=None,region_count=None))
            chips=sorted({v['field'].split('/')[0] for v in fields})
            feedback=self._feedback.snapshot(run_id)
            by_observation={record['identity']['observation']:record for record in feedback.values()}
            current_feedback={}
            for row in rows:
                record=by_observation.get(row['id'])
                if record:
                    expected={**self._feedback_identity(run_id,request_hash,analysis_source,row['field'],commits[row['field']]),'observation':row['id']}
                    record=feedback.get(self._feedback.identity_digest(expected))
                    if record:current_feedback[row['id']]=record
            rows=[{**r,'human_feedback':current_feedback.get(r['id'])} for r in rows] if current_feedback else rows
            self._human_feedback[str(output)]=current_feedback
            self._active_index[str(output)]=active_index
            if observation:group_duplicates=False
            version=self._canonical([active_index,fields,refinement.get('_version'),dedup.get('_version'),current_feedback])
            ranking='hbn_thin_film_evidence_v2' if is_hbn else 'empirical_support_then_joint_v1' if any('empirical_support_tier' in r.get('optical_evidence',{}) for r in rows) else 'joint_evidence_v1'
            view_key=(version,chip,field,minimum_area,group_duplicates,hide_artifacts,observation,ranking,target_band if is_hbn else None)
            def priority(row):return self._hbn_priority(row,refinements) if is_hbn else (-1 if self._human_positive(row) else self._review_tier(row),)
            def matches(row,kind):
                disposition=self._hbn_disposition(row,refinements) if is_hbn else row.get('disposition')
                if self._human_positive(row):disposition='shortlist'
                elif self._human_negative(row):disposition='uncertain'
                filtered=self._filtered(row) or (is_hbn and bool(self._hbn_screen_reasons(row,refinements)))
                if is_hbn and row['id'] not in refinements:filtered=False
                if self._human_positive(row):filtered=False
                elif self._human_negative(row):filtered=True
                return kind=='all' or kind=='rejected' and filtered or kind in ('shortlist','uncertain') and disposition==kind
            def grouped(candidates):
                if not group_duplicates:return candidates
                chosen={}
                for row in candidates:
                    group=groups.get(row['id'],{});key=group.get('id',row['id']);previous=chosen.get(key)
                    better_tier=previous is not None and priority(row)<priority(previous)
                    same_tier=previous is not None and priority(row)==priority(previous)
                    preferred=same_tier and (row['id']==group.get('representative_id') or
                        (previous['id']!=group.get('representative_id') and float(row.get('rank') or 0)>float(previous.get('rank') or 0)))
                    if previous is None or better_tier or preferred:
                        chosen[key]=row
                return list(chosen.values())
            if view_key in self._views:
                self._views.move_to_end(view_key);artifact_count,selections=self._views[view_key]
            else:
                matched=[r for r in rows if (not chip or r['field'].split('/')[0]==chip) and (not field or r['field']==field) and isinstance(r.get('area_um2'),(int,float)) and r['area_um2']>=minimum_area]
                if is_hbn:matched=[r for r in matched if self._hbn_band_matches(r,refinements,target_band)]
                def hidden_artifact(row):
                    if self._human_positive(row):return False
                    return self._hbn_halo_hidden(row,refinements) if is_hbn else refinements.get(row['id'],{}).get('likely_artifact') is True
                artifact_count=sum(hidden_artifact(r) for r in matched)
                if observation:matched=[r for r in rows if r['id']==observation]
                elif hide_artifacts:matched=[r for r in matched if not hidden_artifact(r)]
                if self._score_version!=version:
                    self._scores={};self._score_version=version
                for row in matched:
                    if row['id'] not in self._scores:
                        factors=self._joint_factors(row,refinements.get(row['id'],{}))
                        self._scores[row['id']]=math.prod(factors[:4])
                scores=self._scores
                selections={kind:sorted(grouped([r for r in matched if matches(r,kind)]),
                    key=lambda r:(priority(r),-scores[r['id']],-float(r.get('rank') or 0.),r['field'],r['id'])) for kind in ('shortlist','uncertain','rejected','all')}
                self._views[view_key]=(artifact_count,selections)
                while len(self._views)>4:self._views.popitem(last=False)
            counts={kind:len(value) for kind,value in selections.items()}
            selected=selections[category]
            count=len(selected);offset=min(offset,max(0,((count-1)//limit)*limit))
            result_rows=[]
            for item in selected[offset:offset+limit]:
                try:
                    index_key=self._field_index_keys[str(output),item['field']]
                    row=self._page_row(output,item['field'],commits[item['field']],index_key,item['id'],detail)
                    preview=self._inside(run,row['preview'])
                    if preview.suffix.lower() not in ('.jpg','.jpeg','.png','.webp'): raise ValueError('Invalid preview type')
                    refined=self._refinement(refinement,row['id'],detail)
                    group=groups.get(row['id'])
                    if group and not detail:group={k:group[k] for k in ('id','representative_id','observation_ids','observation_count','status') if k in group}
                    original_reasons=list(row.get('filter_reasons',[]))
                    corrected_size=refinement.get('schema')==2
                    size_assessment={'area_qualified':row.get('area_um2',0)>=25.,'minimum_area_um2':25.,
                        'width_is_diagnostic_not_rejection':True,'material_color_independent':True,
                        'boundary_refined':row['id'] in refinements} if corrected_size else None
                    display_reasons=[r for r in original_reasons if not (corrected_size and r=='below_useful_size' and row.get('area_um2',0)>=25.)]
                    hbn_projection={}
                    if is_hbn:
                        display_reasons=list(dict.fromkeys(display_reasons+self._hbn_screen_reasons(row,refinements)))
                        hbn_projection={'original_disposition':row.get('disposition'),
                            'disposition':self._hbn_disposition(item,refinements),
                            'hbn_screen':refinements.get(row['id'],{}).get('hbn_screen'),
                            'review_pending':row['id'] not in refinements}
                    components=self._joint_score(row,refinements.get(row['id'],{}))
                    if is_hbn:
                        components={**components,'version':'hbn_thin_film_evidence_v2',
                            'target_log_evidence':-self._hbn_priority(row,refinements)[1],
                            'target_band':self._hbn_evidence(row,refinements).get('target_band','unknown'),
                            'ordering':'Thin support, target optical evidence, conditional target band, then soft geometry score.'}
                    human_record=current_feedback.get(row['id'])
                    human_projection={'human_feedback':human_record if human_record and human_record.get('label') else None,
                        'feedback_revision':human_record.get('revision',0) if human_record else 0,
                        'feedback_identity':{**self._feedback_identity(run_id,request_hash,analysis_source,item['field'],commits[item['field']]),'observation':row['id']}}
                    if self._human_positive(item):human_projection['disposition']='shortlist'
                    elif self._human_negative(item):human_projection['disposition']='uncertain'
                    result_rows.append({**row,**hbn_projection,**human_projection,'original_filter_reasons':original_reasons,'display_filter_reasons':display_reasons,
                        'size_assessment':size_assessment,'refinement':refined,'dedup':group,'detail_loaded':bool(detail),
                        'optical_rank':row.get('rank'),'boundary_quality_rank':refinements.get(row['id'],{}).get('rank'),
                        'review_rank':components.get('target_log_evidence',components['combined_score']),'review_score_components':components,
                        'preview_url':'/files/'+quote(preview.relative_to(self.root).as_posix(),safe='/'),
                        'source_field_reference':commits[item['field']]['field_json']})
                except (OSError,ValueError,TypeError,KeyError,StopIteration) as exc:
                    warnings.append(item['field']+': '+str(exc))
            source=None
            if field:
                preview=self._inside(run,field.split('/')[0]+'/tiles/'+field.split('/')[1]+'.jpg')
                source=dict(field=field,preview_url='/files/'+quote(preview.relative_to(self.root).as_posix(),safe='/'),
                    **next(({k:v for k,v in f.items() if k!='field'} for f in fields if f['field']==field),{'state':'not_committed'}))
            return dict(material=request['settings']['material'],material_label='hBN' if request['settings']['material']=='hBN' else 'Graphene',run_id=run_id,sample_id=request['settings'].get('sample_id'),settings=request['settings'],
                acquisition=acquisition,status=status or {'state':'not_started','processed_fields':0,'available_fields':len(fields)},
                analysis_source=analysis_source,
                related_analysis=None if offline else self._related(request),
                stale=bool(stale or ledger_stale or getattr(self,'_annotation_stale',False)),rows=result_rows,filtered_count=count,counts=counts,
                offset=offset,limit=limit,category=category,minimum_area_um2=minimum_area,ranking=ranking,
                target_band=target_band if is_hbn else None,
                chip_ids=chips,fields=sorted(fields,key=lambda v:v['field']) if include_fields else None,fields_version=version,source=source,
                indexed_fields=len(commits),source_region_count=sum(f.get('region_count') or 0 for f in fields),warnings=warnings[:20],
                review_index={'state':'loading' if waiting else 'ready','fields_indexed':len(commits),
                    'fields_waiting':waiting,'progressive':bool(progressive)},
                review_processing={'dedup_available':bool(dedup),'group_duplicates':group_duplicates,'minimum_grouped_area_um2':dedup.get('minimum_area_um2'),
                    'hbn_thin_film_evidence_required':is_hbn,
                    'pending_observations':sum(r['id'] not in refinements for r in rows
                        if is_hbn and isinstance(r.get('area_um2'),(int,float)) and r['area_um2']>=minimum_area),
                    'preliminary_results_available':is_hbn and any(r['id'] not in refinements for r in rows
                        if isinstance(r.get('area_um2'),(int,float)) and r['area_um2']>=minimum_area),
                    'refinement_available':bool(refinement),'refined_observations':len(refinements),'hidden_artifacts':artifact_count if hide_artifacts else 0,
                    'artifact_observations':artifact_count,'hide_artifacts':hide_artifacts},
                refinement_progress=refinement.get('progress'),
                qualification='Saved assessed regions only. Filtered-out regions retain their original uncertain disposition. No material identity or layer assignment is confirmed.')

    def color_data(self, run_id, *, chip='',target_band='both'):
        """Every finite region mean, independent of gallery pagination/area filters."""
        with self._lock:
            # Keep snapshot selection and its compact-index consumption atomic;
            # another request must not evict that analysis between these steps.
            snapshot=self.data(run_id,minimum_area=0,limit=1,detail=False,include_fields=False,chip=chip,target_band=target_band)
            output=str(self._inside(self.root,snapshot['analysis_source']['analysis_relative_path']))
            cache_key=self._canonical({'output':output,'material':snapshot['material'],
                'plan':snapshot['analysis_source'].get('plan_sha256'),'index':self._active_index.get(output,[]),
                'review_version':snapshot['fields_version'],'target_band':snapshot['target_band']})
            if self._color_cache and self._color_cache[0]==cache_key:
                return {**self._color_cache[1],'warnings':snapshot['warnings']}
            values=array.array('f');proposed=[];omitted=0
            for key in self._active_index.get(output,[]):
                rows,summary=self._index[key];field=key[1]
                for row in rows:
                    row={**row,'human_feedback':self._human_feedback.get(output,{}).get(row['id'])}
                    c=row.get('contrast_rgb')
                    if not isinstance(c,list) or len(c)!=3 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in c):
                        omitted+=1;continue
                    selected=(self._hbn_disposition(row,self._hbn_screens.get(output,{})) if snapshot['material']=='hBN' else row.get('disposition'))=='shortlist'
                    if self._human_positive(row):selected=True
                    elif self._human_negative(row):selected=False
                    if snapshot['material']=='hBN':selected=selected and self._hbn_band_matches(row,self._hbn_screens.get(output,{}),target_band)
                    values.extend([*c,float(row['area_um2']),float(selected)])
                    if selected:proposed.append({'id':row['id'],'field':field,'contrast_rgb':c,'area_um2':row['area_um2']})
            if sys.byteorder!='little':values.byteswap()
            prior=Path(output).parent/'model-comparison'/'contrast-priors.png'
            result={'target_band':snapshot['target_band'],'material':snapshot['material'],'material_label':snapshot['material_label'],'run_id':run_id,'count':len(values)//5,'stride':5,'encoding':'little-endian-float32-base64',
                'columns':['red','green','blue','area_um2','proposed'],
                'points':base64.b64encode(values.tobytes()).decode('ascii'),'proposals':proposed,
                'omitted_nonfinite':omitted,'warnings':snapshot['warnings'],
                'empirical_trajectory':snapshot['analysis_source'].get('empirical_trajectory'),
                'prior_plot_url':'/files/'+quote(prior.relative_to(self.root).as_posix(),safe='/') if prior.is_file() else None,
                'qualification':'One point per saved region mean, including small and unselected regions. All readable records; no point subsampling. These are not individual camera pixels or confirmed material identity.'}

            self._color_cache=(cache_key,result)
            return result
