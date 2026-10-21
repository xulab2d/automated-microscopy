"""Read-only replay of the October 15 whole-chip storage forecast stop."""
from pathlib import Path
import json
import statistics
import sys
from types import SimpleNamespace
from PIL import Image

workspace=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(workspace/'research/software-next-20261009'))
from flakefinder.chip_campaign import estimate_campaign_storage,focus_geometry_diagnostics

project=Path(json.loads((workspace/'configs/storage.json').read_text(encoding='utf-8-sig'))['project_root'])
run=project/'data/scan-20261015-215627-bb0a2529'
batch=json.loads((run/'batch.json').read_bytes())
config=json.loads((run/'scan-config.json').read_bytes())
plan=json.loads((Path(__file__).parent/'focus-plan-215627.json').read_bytes())['plan']
original=batch['boundary_attempts'][0]['storage_preflight']
free=original['volumes'][0]['free_bytes']
repaired=estimate_campaign_storage(config,plan,original['raw_frame_bytes'],run/'chip001',
    geometry=focus_geometry_diagnostics(plan),disk_usage=lambda path:SimpleNamespace(free=free))
sizes=[];compression=set()
for path in sorted((run/'discovery/candidate001/frames').glob('*.tiff')):
    sizes.append(path.stat().st_size)
    with Image.open(path) as im:compression.add(im.tag_v2.get(259))
assert sizes and repaired['can_start'] and not repaired['sufficient']
result={'run_id':run.name,'original_state':batch['state'],'tile_count':plan['tile_count'],
    'original_preflight':original,'repaired_preflight':repaired,
    'saved_raw_samples':len(sizes),'tiff_compression_tags':sorted(compression),
    'median_raw_file_bytes':statistics.median(sizes),'maximum_raw_file_bytes':max(sizes),
    'projected_raw_gib_at_median':statistics.median(sizes)*plan['tile_count']/2**30,
    'projected_raw_gib_at_maximum':max(sizes)*plan['tile_count']/2**30,
    'qualification':'Offline admission replay at the original free-space reading. File-size projection excludes previews/derived files and is not a completion guarantee. No hardware calls or source data edits.'}
output=Path(__file__).parent/'storage-replay-215627.json'
output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:result[k] for k in ('run_id','tile_count','saved_raw_samples','projected_raw_gib_at_median')}))
print(json.dumps({'can_start':repaired['can_start'],'full_forecast_fits':repaired['sufficient']}))
