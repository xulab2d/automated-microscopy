"""Read saved JSON only; no segmentation, camera, source-file or UI changes."""
from collections import Counter
from pathlib import Path
import gzip
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def main():
    package = ROOT/'laptop-viewer'
    manifest = json.loads((package/'snapshot.json').read_text(encoding='utf-8'))
    bins = {name:Counter() for name in ('below20','20to25','25to50','50to100','100plus')}
    measurement_reasons = Counter();warnings = Counter();example_fields = [];selected_unmeasured = []
    focus = {'chip001/tile_'+str(n).zfill(6) for n in (139,141,219,239,199,369)}
    for key,relative in manifest['field_files'].items():
        raw = (package/relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['artifacts'][relative]:
            raise ValueError('Saved viewer shard changed: '+key)
        field = json.loads(gzip.decompress(raw))
        for row in field['rows']:
            area = row['area_um2'];measurement = row.get('local_contrast_measurement') or {}
            group = bins['below20' if area<20 else '20to25' if area<25 else '25to50' if area<50 else '50to100' if area<100 else '100plus']
            group['source_objects'] += 1
            group['selected'] += int(row['gallery_selected'])
            group['measured'] += int(measurement.get('measurement_status')=='measured')
            group['width_ge5'] += int(row.get('usable_width_um',0)>=5)
            measurement_reasons[measurement.get('reason','not_recorded')] += 1
            warnings.update(row.get('evidence_warnings',[]))
            if row['gallery_selected'] and measurement.get('measurement_status')!='measured':
                selected_unmeasured.append({'tile_key':key,'id':row['id'],'area_um2':area,'reason':measurement.get('reason')})
        if key in focus:
            samples=[]
            for row in sorted(field['rows'],key=lambda x:x['area_um2'],reverse=True)[:4]:
                samples.append({k:row.get(k) for k in ('id','instance_label','bbox_px','area_um2','usable_width_um',
                    'solidity','gallery_selected','evidence_warnings','filter_reasons','local_contrast_measurement')})
            example_fields.append({'key':key,'status':field['analysis_status'],'counts':field['counts'],
                'source_sha256':field['source_sha256'],'top_area_objects':samples})
    controls=json.loads((ROOT/'data/manual-visual-audit-01/benchmark.json').read_text())
    coverage=json.loads((ROOT/'data/manual-visual-audit-01/mask-coverage.json').read_text())
    coverage={r['id']:r['comparisons'].get('component_repair') for r in coverage['comparisons']}
    compact=[{**{k:r.get(k) for k in ('id','label','tile_id','source_image','source_file_sha256',
        'polygon_px','polygon_area_um2','crop_bounds_px','edge_uncertainty_px','description')},
        'existing_component_repair':coverage.get(r['id'])} for r in controls['annotations']]
    result={'scope':'Read-only saved metadata audit; existing independent visual controls, not a new recall benchmark.',
        'snapshot_created_at':manifest['created_at'],'image_count':manifest['image_count'],
        'field_statuses':manifest['field_statuses'],'object_counts':manifest['counts'],
        'area_bins_um2':bins,'measurement_reasons':measurement_reasons,'evidence_warnings':warnings,
        'selected_unmeasured':selected_unmeasured,'representative_fields':example_fields,
        'earlier_independent_controls':compact}
    (OUT/'saved-data-audit.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('field_statuses','area_bins_um2','measurement_reasons','evidence_warnings')},indent=2))


if __name__=='__main__':
    main()
