"""Read-only patch/source verification plus compact review report, no hardware."""
from pathlib import Path
import hashlib,json,subprocess,time

HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[2]
manifest=json.loads((HERE/'source-manifest.json').read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
production={name:sha(ROOT/name)==digest for name,digest in manifest['production_baseline_sha256'].items()}
staged={name:sha(HERE/name)==digest for name,digest in manifest['staged_source_sha256'].items()}
assert all(production.values()) and all(staged.values()),'Source hash changed; review explicitly.'
check=subprocess.run(['git','apply','--check','--ignore-space-change',str(HERE/'integration.patch')],cwd=ROOT,capture_output=True,text=True)
assert check.returncode==0,check.stderr
replay=json.loads((HERE/'replay-results.json').read_text());rows=replay['rows']
replay_algorithms_unchanged={name:replay['staged_sources'][name]==manifest['staged_source_sha256'][name]
    for name in ('flakefinder/sparse_edges.py','flakefinder/photometric_edges.py','flakefinder/sparse_acquisition.py')}
assert all(replay_algorithms_unchanged.values()),'Detection algorithm changed; previous replay cannot be reused.'
result={'schema':1,'verified_at':time.time(),'scope':'Isolated review only. No hardware, production mutation, or migration.',
    'production_baselines_unchanged':production,'staged_source_hashes_match':staged,
    'read_only_patch_check':{'exit_code':check.returncode,'stderr':check.stderr},
    'replay':{'frames':len(rows),'accepted':sum(bool(row['edge'])for row in rows),
        'old_accepted_geometry_unchanged':sum(row['previously_accepted_geometry_unchanged'] is True for row in rows),
        'raw_refined_new_proposals':sum(row['validation'].get('candidate_unchanged') is False for row in rows if row['edge']),
        'rejected':[row['attempt']+'/'+row['image']for row in rows if not row['edge']],
        'fits':{key:{'status':value['status'],'lines':len(value['lines']),
            'registered_consistency':value['registered_consistency']['accepted']}for key,value in replay['fits'].items()},
        'runtime_s':replay['runtime_s']},
    'patch_sha256':sha(HERE/'integration.patch'),'replay_sha256':sha(HERE/'replay-results.json'),
    'replay_algorithm_hashes_unchanged':replay_algorithms_unchanged,
    'compatibility_followup':{'existing_tests_passed_with_explicit_v2_fixtures':44,'new_cli_tests_passed':8,
        'new_legacy_identity_test_passed':1,'remaining_failures':0,
        'unmodified_existing_coverage':['campaign envelope/storage preflight','legacy known-chip resume','edge focus anchors'],
        'intentional_contract_changes':['Explicit20x dark-holder profile required','Focus tests mock validated_coherent_edge','Legacy v1 discovery continuation and seed recovery rejected before connection'],
        'qualification':'Completed bounded test invocations; CLI fixture schema typo corrected and all8 CLI tests rerun. No additional raw-image replay.'},
    'focused_tests_recorded':{'passed':10,'seconds':1.77,'command':'.venv/Scripts/python.exe -m pytest research/discovery-gap-repair/integration/tests/test_integration.py -q --basetemp data/test-temp-integration-v2-isolated-02',
        'qualification':'Result recorded from the completed focused test invocation; this verification script does not rerun tests.'},
    'migration':'Legacy in-place discovery continuation deliberately incompatible; reviewed new lineage or separately implemented migration required.'}
(HERE/'review-summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
