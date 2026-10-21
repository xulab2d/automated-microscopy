"""Generate isolated review copies and patch. Never modifies acquisition sources."""
from pathlib import Path
import difflib, hashlib, json

HERE = Path(__file__).resolve().parent
RESEARCH = HERE.parent
ROOT = HERE.parents[2]
baselines = {}
pinned_path=HERE/'source-manifest.json'
pinned=json.loads(pinned_path.read_text(encoding='utf-8'))['production_baseline_sha256'] if pinned_path.exists() else {}

def source(name):
    path = ROOT / name
    baselines[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if name in pinned and baselines[name]!=pinned[name]:
        raise RuntimeError('Production baseline changed; review/rebase explicitly instead of silently regenerating: '+name)
    return path.read_text(encoding='utf-8')

def change(text, old, new, count=1):
    assert text.count(old) == count, (old[:120], text.count(old), count)
    return text.replace(old, new)

def write(name, text):
    path = HERE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    compile(text, name, 'exec')
    path.write_text(text, encoding='utf-8')

photo = (RESEARCH/'photometric_validation.py').read_text(encoding='utf-8')
photo += '\n\n' + (HERE/'profile_helpers.py.txt').read_text(encoding='utf-8')
write('flakefinder/photometric_edges.py', photo)

edges = source('flakefinder/sparse_edges.py')
helper = (RESEARCH/'standard_hough_fallback.py').read_text(encoding='utf-8')
helper = helper[helper.index('def _standard_hough_candidate'):]
edges = change(edges, 'def coherent_edge(labels, position_um, source_shape, matrix):',
               helper+'\n\ndef _label_edge(labels, position_um, source_shape, matrix, *, standard_fallback=False):')
edges = change(edges, '    if lines is None:\n        return None\n    yy, xx = np.mgrid[:h, :w]\n    best = None\n    for x1, y1, x2, y2 in lines.reshape(-1, 4):',
               '    yy, xx = np.mgrid[:h, :w]\n    best = None\n    for x1, y1, x2, y2 in (() if lines is None else lines.reshape(-1, 4)):')
edges = change(edges, '    if best is None:\n        return None\n    _, points, normal, ps, ph, residual = best',
               '    if best is None and standard_fallback:\n        best = _standard_hough_candidate(edges, s, b)\n    if best is None:\n        return None\n    _, points, normal, ps, ph, residual = best')
edges = change(edges, '\ndef _clip(', '\n'+(HERE/'edge_helpers.py.txt').read_text(encoding='utf-8')+'\ndef _clip(')
write('flakefinder/sparse_edges.py', edges)

acq = source('flakefinder/sparse_acquisition.py')
acq = change(acq, 'from .sparse_edges import coherent_edge,fit_outline',
    'from .sparse_edges import validated_coherent_edge,fit_validated_outline\nfrom .photometric_edges import ALGORITHM_VERSION,assess_edge_profile,spatial_bracket_evidence')
start = acq.index('def _bracket_side('); end = acq.index('\ndef discover_sparse_outline', start)
acq = acq[:start]+'''def _bracket_side(record,allow_fraction=False):
    # The legacy aggregate-only switch no longer bypasses spatial evidence.
    # Original classifications remain intact; inference advances brackets only.
    if record['classification']!='unknown':return record['classification']
    spatial=record.get('spatial_bracket_evidence',{'side':'unknown'})
    if spatial['side'] in ('substrate','holder'):record['bracket_inference']=spatial.copy()
    return spatial['side']

'''+acq[end:]
gap=(RESEARCH/'proposed_gap_probe.py').read_text(encoding='utf-8')
gap=gap[gap.index('def _gap_probe_direction'):]
gap=gap.replace('remaining, max_attempts=3):','remaining, fit_model, max_attempts=3):')
gap=gap.replace('fit_outline(edges, seed, bounds, margin)','fit_model()').replace('fit_outline(edges,seed,bounds,margin)','fit_model()')
acq=change(acq,'def discover_sparse_outline(',gap+'\n\ndef discover_sparse_outline(')
acq=change(acq,'focus_search_bounds_um=None,allow_source_change=False):','focus_search_bounds_um=None,allow_source_change=False,edge_validation_profile=None):')
acq=change(acq,"    payload={'schema':1,'algorithm':'sparse_coarse_bracket_halfplanes_v1'", "    profile_report=assess_edge_profile(edge_validation_profile,reference_frame)\n    payload={'schema':2,'algorithm':ALGORITHM_VERSION")
acq=change(acq,"        'edge_model_sha256':file_hash(Path(__file__).with_name('sparse_edges.py')),'seed_xy_um':seed.tolist(),", "        'edge_model_sha256':file_hash(Path(__file__).with_name('sparse_edges.py')),\n        'photometric_model_sha256':file_hash(Path(__file__).with_name('photometric_edges.py')),\n        'edge_validation_profile':edge_validation_profile,'seed_xy_um':seed.tolist(),")
acq=change(acq,"    if focus_search_bounds_um is not None:\n        policy=", "    if not profile_report['supported']:\n        state.update(state='unresolved',reason='unsupported_edge_profile',profile_validation=profile_report,\n                     estimated_outline={'status':'unresolved','reason':'unsupported_edge_profile','polygon_um':[]})\n        atomic_json(destination,state);atomic_json(folder/'estimated-outline.json',state['estimated_outline'])\n        return state\n    if focus_search_bounds_um is not None:\n        policy=")
acq=change(acq,'    by_xy={};edges=[]','    by_xy={};edges=[];observations=[]')
acq=change(acq,"        evidence.add(labels,record['position_um'],payload['source_shape'],transform)", "        observations.append((record['image'],labels,record['position_um'][:2]))\n        evidence.add(labels,record['position_um'],payload['source_shape'],transform)", count=2)
acq=change(acq,"        by_xy[tuple(np.round(record['requested_xy_um'],3))]=record", "        record['spatial_bracket_evidence']=spatial_bracket_evidence(labels,record['classification'])\n        by_xy[tuple(np.round(record['requested_xy_um'],3))]=record")
acq=change(acq,"    def save():state['updated_at']=time.time();atomic_json(destination,state)", "    def save():state['updated_at']=time.time();atomic_json(destination,state)\n    def fit_model():\n        return fit_validated_outline(edges,seed,options.search_bounds_um,options.margin_um,observations,transform.matrix,payload['source_shape'])\n    def save_ray(name,ray):state['rays'][name]=ray;save()")
acq=change(acq,"        labels=classify_boundary(frame,reference,reference_rgb,boundary_options);side,fractions=_side(labels)", "        capture_profile=assess_edge_profile(edge_validation_profile,frame)\n        if not capture_profile['supported']:raise AcquisitionError('Sparse edge capture profile changed: '+capture_profile['reason'])\n        labels=classify_boundary(frame,reference,reference_rgb,boundary_options);side,fractions=_side(labels)")
acq=change(acq,'        edge=coherent_edge(labels,[frame.position.x,frame.position.y],frame.rgb.shape,transform.matrix)', '        edge,edge_validation=validated_coherent_edge(frame,labels,transform.matrix,edge_validation_profile)')
acq=change(acq,"                'classification':side,'fractions':fractions,'edge':edge,'hashes'", "                'classification':side,'fractions':fractions,'edge':edge,'edge_validation':edge_validation,\n                'spatial_bracket_evidence':spatial_bracket_evidence(labels,side),'hashes'")
acq=acq.replace("        evidence.add(labels,record['position_um'],frame.rgb.shape,transform)", "        observations.append((image,labels,record['position_um'][:2]))\n        evidence.add(labels,record['position_um'],frame.rgb.shape,transform)",1)
acq=change(acq,'            save()\n    except _Budget:', '''            save()
        _complete_missing_edge_directions(edges,seed,options.search_bounds_um,field,
            options.coarse_step_um,options.margin_um,acquire,ray_edge,_bracket_side,check,save_ray,
            lambda:options.max_probes-len(state['records']),fit_model)
    except _Budget:''')
acq=change(acq,'        result=fit_outline(edges,seed,options.search_bounds_um,options.margin_um)', '        result=fit_model()')
write('flakefinder/sparse_acquisition.py',acq)

campaign=source('flakefinder/chip_campaign.py')
campaign=change(campaign,'from .sparse_acquisition import SparseEdgeOptions,discover_sparse_outline,acquire_interior_support',
    'from .sparse_acquisition import SparseEdgeOptions,discover_sparse_outline,acquire_interior_support\nfrom .photometric_edges import ALGORITHM_VERSION,assess_edge_profile')
campaign=change(campaign,"    if recover_seed_rejection and not continue_discovery:raise ValueError('Seed rejection recovery requires explicit continue_discovery.')", "    if recover_seed_rejection and not continue_discovery:raise ValueError('Seed rejection recovery requires explicit continue_discovery.')\n    if recover_seed_rejection:raise SafetyError('Legacy seed-rejection recovery is not compatible with the v2 discovery contract; preserve the recorded lineage.')")
campaign=change(campaign,"    reference=SubstrateReference.from_frame(reference_frame)\n    continuation_saved=", '''    reference=SubstrateReference.from_frame(reference_frame)
    edge_profile=config.get('chip_discovery',{}).get('edge_validation_profile')
    edge_contract=assess_edge_profile(edge_profile,reference_frame)
    if not edge_contract['supported']:raise SafetyError('Discovery unresolved before connection: '+edge_contract['reason'])
    discovery_contract={'algorithm':ALGORITHM_VERSION,'profile':edge_profile,
        'edge_model_sha256':file_hash(Path(__file__).with_name('sparse_edges.py')),
        'photometric_model_sha256':file_hash(Path(__file__).with_name('photometric_edges.py'))}
    continuation_saved=''')
campaign=change(campaign,"    if continue_discovery:\n        saved_focus=", "    if continue_discovery:\n        if continuation_saved.get('discovery_contract')!=discovery_contract:\n            raise SafetyError('Discovery algorithm/profile transition requires a separate reviewed campaign lineage; legacy identities are not rewritten.')\n        saved_focus=")
campaign=change(campaign,"        initial_state=json.loads((initial_discovery/'discovery.json').read_text(encoding='utf-8'))", "        initial_state=json.loads((initial_discovery/'discovery.json').read_text(encoding='utf-8'))\n        _require_discovery_contract(initial_state,discovery_contract)")
campaign=change(campaign,"            'algorithm_source_sha256':file_hash(__file__),\n            'started_at'", "            'algorithm_source_sha256':file_hash(__file__),'discovery_contract':discovery_contract,\n            'started_at'")
campaign=change(campaign,"                saved_discovery=json.loads((discovery_folder/'discovery.json').read_text(encoding='utf-8'))", "                saved_discovery=json.loads((discovery_folder/'discovery.json').read_text(encoding='utf-8'))\n                _require_discovery_contract(saved_discovery,discovery_contract)")
# Only the three sparse-discovery callsites have this keyword expression.
campaign=change(campaign,'focus_search_bounds_um=full_options.focus_search_bounds_um)', 'focus_search_bounds_um=full_options.focus_search_bounds_um,edge_validation_profile=edge_profile)',count=3)
idx=campaign.index('def run_chip_campaign(')
campaign=campaign[:idx]+'''def _require_discovery_contract(saved,contract):
    if (saved.get('algorithm')!=contract['algorithm'] or saved.get('edge_validation_profile')!=contract['profile']
            or saved.get('edge_model_sha256')!=contract['edge_model_sha256']
            or saved.get('photometric_model_sha256')!=contract['photometric_model_sha256']):
        raise SafetyError('Saved discovery has a different algorithm/profile; preserve its identity and reanalyse separately.')


'''+campaign[idx:]
write('flakefinder/chip_campaign.py',campaign)

cli=source('scripts/scan_chip_campaign.py')
cli=change(cli,"    args=p.parse_args();cfg=load_config(args.config)", "    p.add_argument('--edge-validation-profile',type=Path,help='Explicit JSON dark-holder/manual-20x profile for new v2 discovery; legacy campaign migration is not implicit.')\n    args=p.parse_args();cfg=load_config(args.config)\n    if args.edge_validation_profile:\n        cfg.setdefault('chip_discovery',{})['edge_validation_profile']=json.loads(args.edge_validation_profile.read_text(encoding='utf-8-sig'))")
write('scripts/scan_chip_campaign.py',cli)

cli=source('scripts/discover_chip_boundaries.py')
cli=change(cli,'from flakefinder.model import atomic_json','from flakefinder.model import atomic_json,SafetyError,Cancelled\nfrom flakefinder.photometric_edges import assess_edge_profile,ALGORITHM_VERSION\nfrom flakefinder.chip_discovery import file_hash')
cli=change(cli,"        if name=='edges':", "        if name=='edges':\n            sub.add_argument('--edge-validation-profile',type=Path,help='Explicit dark-holder/manual-20x JSON profile; required for sparse edges unless supplied in config.chip_discovery.')")
cli=change(cli,"    cfg=load_config(args.config);reference=load_frame(args.reference);transform=transform_from_config(cfg)", '''    cfg=load_config(args.config);reference=load_frame(args.reference);transform=transform_from_config(cfg)
    transform.validate_frame(reference)
    if not transform.calibrated:raise SafetyError('A measured camera transform is required before connection.')
    if (args.output/'STOP').exists():raise Cancelled('Boundary STOP exists; no hardware connection opened.')
    edge_profile=None
    if args.command=='edges' and args.method=='sparse':
        edge_profile=(json.loads(args.edge_validation_profile.read_text(encoding='utf-8-sig'))
            if args.edge_validation_profile else cfg.get('chip_discovery',{}).get('edge_validation_profile'))
        report=assess_edge_profile(edge_profile,reference)
        if not report['supported']:raise SafetyError('Sparse discovery unresolved before connection: '+report['reason'])
        if args.resume and (args.output/'discovery.json').exists():
            saved=json.loads((args.output/'discovery.json').read_text(encoding='utf-8'))
            import flakefinder.sparse_edges as edge_module
            import flakefinder.photometric_edges as photo_module
            if (saved.get('algorithm')!=ALGORITHM_VERSION or saved.get('edge_validation_profile')!=edge_profile
                    or saved.get('edge_model_sha256')!=file_hash(edge_module.__file__)
                    or saved.get('photometric_model_sha256')!=file_hash(photo_module.__file__)):
                raise SafetyError('Legacy/different sparse discovery cannot resume under this profile; preserve its identity.')''')
cli=change(cli,"    scope=None\n    try:", '''    # Validate options before opening an adapter, including the hard32 budget.
    if args.command=='edges':
        seed=args.seed_xy or [reference.position.x,reference.position.y]
        options=(SparseEdgeOptions(args.bounds,args.coarse_step_um,args.max_probes,args.cell_um,args.dark_holder_ratio,args.margin_um,policy)
            if args.method=='sparse' else BoundaryOptions(args.bounds,args.cell_um,args.overlap,args.max_probes,
                dark_holder_ratio=args.dark_holder_ratio,focus_policy_id=policy))
    scope=None
    try:''')
cli=change(cli,"            seed=args.seed_xy or [reference.position.x,reference.position.y]\n",'')
cli=change(cli,"                options=SparseEdgeOptions(args.bounds,args.coarse_step_um,args.max_probes,args.cell_um,args.dark_holder_ratio,args.margin_um,policy)\n",'')
cli=change(cli,"                options=BoundaryOptions(args.bounds,args.cell_um,args.overlap,args.max_probes,\n                                        dark_holder_ratio=args.dark_holder_ratio,focus_policy_id=policy)\n",'')
cli=change(cli,"resume=args.resume,continue_unresolved=args.continue_unresolved,before_capture=focus,callback=progress)", "resume=args.resume,continue_unresolved=args.continue_unresolved,before_capture=focus,callback=progress,\n                                               edge_validation_profile=edge_profile)")
write('scripts/discover_chip_boundaries.py',cli)

# Fixture updates are isolated copies, included in the reviewed patch.
test=source('tests/test_sparse_acquisition.py')
test=test.replace('from test_chip_discovery import ImageStage,RECT','from test_chip_discovery import RECT\nfrom sparse_profile_fixture import ImageStage,profile_for')
test=test.replace('scope.transform,reference,tmp_path,options','scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference)')
test=test.replace("SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20))", "SparseEdgeOptions([20,1200,20,1100],coarse_step_um=400,cell_um=5,margin_um=20),edge_validation_profile=profile_for(reference))")
write('tests/test_sparse_acquisition.py',test)
test=source('tests/test_sparse_edge_focus.py')
test=test.replace('from test_chip_discovery import ImageStage,RECT','from test_chip_discovery import RECT\nfrom sparse_profile_fixture import ImageStage,profile_for')
test=change(test,"    original=sparse.coherent_edge\n    monkeypatch.setattr(sparse,'coherent_edge',lambda *args,**kwargs:original(*args,**kwargs) if scope.did_focus else None)", "    original=sparse.validated_coherent_edge\n    monkeypatch.setattr(sparse,'validated_coherent_edge',lambda *args,**kwargs:original(*args,**kwargs) if scope.did_focus else (None,{'accepted':False,'reason':'synthetic defocus'}))")
test=test.replace('scope.transform,reference,tmp_path,options','scope.transform,reference,tmp_path,options,edge_validation_profile=profile_for(reference)')
write('tests/test_sparse_edge_focus.py',test)
test=source('tests/test_chip_campaign_continuation.py')
test=test.replace('import flakefinder.chip_campaign as campaign','import flakefinder.chip_campaign as campaign\nfrom sparse_profile_fixture import profile_for,campaign_contract,discovery_contract')
test=change(test,"metadata={'white_level':4095,'source':'TEST'})", "metadata={'white_level':4095,'source':'TEST','objective_position':2,'objective':{'magnification':20},\n                          'camera_resources':{'fixed':1},'camera_job_template':'synthetic fixed20x','illumination_percent':90})")
test=change(test,"    search=campaign.plan_campaign_search(bounds,60,[50,50],10)", "    cfg['chip_discovery']={'edge_validation_profile':profile_for(frame)}\n    search=campaign.plan_campaign_search(bounds,60,[50,50],10)")
test=change(test,"        'algorithm_source_sha256':'old-code','chip_states':{}", "        'algorithm_source_sha256':'old-code','discovery_contract':campaign_contract(frame),'chip_states':{}")
test=change(test,"atomic_json(discovery/'discovery.json',{'state':'estimated','records':[],", "atomic_json(discovery/'discovery.json',{**discovery_contract(frame),'state':'estimated','records':[],")
start=test.index('def test_explicit_seed_only_failure_recovery_replays_and_keeps_budget(')
end=test.index("\n\n@pytest.mark.parametrize('change'",start)
test=test[:start]+'''def test_legacy_seed_only_recovery_is_rejected_without_connection_or_identity_rewrite(failed_seed_campaign,monkeypatch):
    saved,folder=failed_seed_campaign;cfg,reference,out,options,frame,transform,search=saved
    before=(out/'batch.json').read_bytes();original=(folder/'discovery.json').read_bytes()
    monkeypatch.setattr(campaign,'connect',lambda config:pytest.fail('Legacy migration connected hardware'))
    with pytest.raises(SafetyError,match='Legacy seed-rejection recovery'):
        campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,
            allow_source_change=True,recover_seed_rejection=True)
    assert (out/'batch.json').read_bytes()==before and (folder/'discovery.json').read_bytes()==original
'''+test[end:]
test+='''

def test_legacy_campaign_discovery_identity_requires_separate_reviewed_migration(saved_campaign,monkeypatch):
    cfg,reference,out,options,frame,transform,search=saved_campaign
    state=json.loads((out/'batch.json').read_text());state.pop('discovery_contract')
    atomic_json(out/'batch.json',state);before=(out/'batch.json').read_bytes()
    monkeypatch.setattr(campaign,'connect',lambda cfg:pytest.fail('Legacy algorithm transition opened hardware'))
    with pytest.raises(SafetyError,match='separate reviewed campaign lineage'):
        campaign.run_chip_campaign(cfg,reference,out,[0,100,0,100],options,continue_discovery=True,allow_source_change=True)
    assert (out/'batch.json').read_bytes()==before
'''
write('tests/test_chip_campaign_continuation.py',test)

files=list(baselines)+['flakefinder/photometric_edges.py','tests/sparse_profile_fixture.py','tests/test_sparse_cli_profile.py']
patch=''
for name in files:
    old=(ROOT/name).read_text(encoding='utf-8') if name in baselines else ''
    new=(HERE/name).read_text(encoding='utf-8')
    patch+=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),
        fromfile='a/'+name if old else '/dev/null',tofile='b/'+name))
(HERE/'integration.patch').write_text(patch,encoding='utf-8')
(HERE/'source-manifest.json').write_text(json.dumps({'schema':1,'algorithm':'sparse_photometric_halfplanes_v2',
    'production_baseline_sha256':baselines,'staged_source_sha256':{name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in files},
    'production_modified':False,'migration':'Legacy discoveries/campaign continuation rejected; separate lineage required.'},indent=2),encoding='utf-8')
print(json.dumps({'staged_files':files,'patch':str(HERE/'integration.patch')}))
