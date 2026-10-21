"""Verify isolated saved outputs and publish one index without rewriting them."""
from audit_saved_outputs import *
from flakefinder.staged_features import _deduplicate_catalog
import shutil

def main():
    winning={};versions=[]
    for name in ('recovery-v1','recovery-v2'):
        folder=OUT/name;analysis=read(folder/'analysis.json');pipeline=read(folder/'pipeline.json')
        if analysis['analysis_state']!='complete':raise ValueError('Recovery is not finished.')
        versions.append({'folder':str(folder),'fingerprint':pipeline['fingerprint'],'analysis_sha256':sha(folder/'analysis.json'),
          'completed_fields':analysis['completed_tiles'],'source_masks':analysis['raw_regions']})
        for source,digest in pipeline['source_code_sha256'].items():
            if sha(ROOT/source)!=digest:raise ValueError('Source no longer matches recorded recovery fingerprint.')
            target=folder/'source-code'/source;target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists() and sha(target)!=digest:raise ValueError('Archived source collision.')
            if not target.exists():shutil.copyfile(ROOT/source,target)
        for path in (folder/'fields').glob('*.json'):
            record=read(path);key=record['tile_key']
            if key in winning and winning[key]['state']=='recovered':raise ValueError('Supplementary recovery must not overwrite successful masks.')
            entry={'tile_key':key,'state':record['state'],'record_file':str(path),'record_sha256':sha(path),'version':name}
            if record['state']=='recovered':
                tile=record['tile'];labels=cv2.imread(str(folder/tile['instance_mask']),-1)
                if sha(folder/tile['instance_mask'])!=record['mask_sha256']:raise ValueError('Saved mask hash mismatch.')
                if sha(folder/tile['support_masks'])!=record['report']['support_sha256']:raise ValueError('Saved support hash mismatch.')
                support=np.load(folder/tile['support_masks'])
                if np.any((labels>0)&~support['valid_domain']):raise ValueError('Instance outside observed domain.')
                if np.any(support['clear_local_substrate']&~support['background_samples']):raise ValueError('Annulus outside measured substrate.')
                if np.any(support['background_samples']&~support['valid_domain']):raise ValueError('Background fit sample outside chip domain.')
                counts=np.bincount(labels.ravel())
                for row in record['rows']:
                    if counts[row['instance_label']]!=row['mask_area_pixels']:raise ValueError('Saved mask/row area mismatch.')
                    if row['thin_reference_evidence']['compatible'] is not None:raise ValueError('Mean-color flag was promoted to thin classification.')
                entry.update(source_masks=tile['proposal_count'],selected_observations=tile['selected_count'],
                  rejected_observations=tile['rejected_count'],instance_mask=str(folder/tile['instance_mask']),
                  support_masks=str(folder/tile['support_masks']),normalization_seconds=record['report']['normalization_seconds'],
                  segmentation_seconds=record['report']['segmentation_seconds'])
            else:entry.update(reason=record['reason'],exclusion_kind=record['exclusion_kind'],recorded_substrate_fraction=record['recorded_substrate_fraction'])
            winning[key]=entry
    records=list(winning.values());recovered=[r for r in records if r['state']=='recovered'];excluded=[r for r in records if r['state']=='excluded']
    rows=[row for entry in recovered for row in read(entry['record_file'])['rows']]
    catalog=_deduplicate_catalog([r for r in rows if r['selected_for_review']])
    original=read(OUT/'recovery-v1/original-failures.json')
    if len(records)!=len(original['failures']):raise ValueError('Not every original failure has a recorded outcome.')
    result={'schema':1,'completed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_folder':str(CHIP),
      'versions':versions,'original_failed_fields':len(original['failures']),'recovered_fields':len(recovered),
      'excluded_fields':len(excluded),'off_chip_exclusions':sum(r['exclusion_kind']=='off_chip' for r in excluded),
      'on_chip_support_or_margin_exclusions':sum(r['exclusion_kind']!='off_chip' for r in excluded),
      'source_masks':len(rows),'selected_observations':sum(r['selected_for_review'] for r in rows),
      'rejected_observations':sum(not r['selected_for_review'] for r in rows),'deduplicated_review_catalog':len(catalog),
      'credible_thin_classifications':0,'mean_color_direction_matches':sum(r['mean_color_reference_evidence'].get('compatible') is True for r in catalog),
      'saved_mask_domain_and_annulus_checks':'passed for every recovered field','saved_mask_row_area_checks':'passed for every source object',
      'source_code_archived':True,'production_changed':False,'hardware_used':False,
      'outcomes':records,'limitations':['Broad feature geometry includes debris; this is normalization recovery, not a material classifier.',
      'Observed raster footprint and the unchanged12um margin exclude some real edge objects.',
      'Five remaining narrow/interrupted supports cannot satisfy spatial holdout validation.',
      'Two more substrate slivers have no usable interior after the edge margin.',
      'Single-plane fields span about16um focus height across the tilted specimen; thin-feature recall is unvalidated.',
      'Catalog centroid-pair dedup can miss extended-object overlaps; all observations remain available.']}
    write('recovery-result.json',result)
    write('recovery-catalog.json',{'source_folder':str(CHIP),'scope':'Only recovered fields, separate from original production catalog','flakes':catalog})
    print(json.dumps({k:v for k,v in result.items() if k not in ('outcomes','versions','limitations')},indent=2))
    print('Exclusions',[(r['tile_key'],r['reason']) for r in excluded])
if __name__=='__main__':main()
