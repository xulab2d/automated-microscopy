"""Read-only before/after candidate and observed-region overlap audit."""
from pathlib import Path
import argparse,json,hashlib
import numpy as np
import analyze_graphene_run as driver

def verified_payload(folder,entry):
    path=folder/entry['field_json'];blob=driver.retry_read(path.read_bytes)
    if hashlib.sha256(blob).hexdigest()!=entry['hashes'][entry['field_json']]:raise ValueError('Changed field payload')
    return path,json.loads(blob)


def compare(before,after,observations):
    old=driver.read(before/'ledger.json');new=driver.read(after/'ledger.json');obs=driver.read(observations)
    samples=obs['first_region']['consistency']+obs['second_region']['saved_points']+obs['third_region']['saved_points']
    negative=set(obs['second_region']['inferred_outlier_ids']);bytile={}
    for sample in samples:bytile.setdefault(sample['id'].split('/')[1],[]).append(sample)
    output={'schema':1,'before':str(before),'after':str(after),'after_plan_sha256':driver.digest(after/'plan.json'),
        'point_correspondence':'Recorded IDs inferred from conversation; overlap links observations, not independent verified identities.',
        'fields':{},'observations':[],'promotions':[],'unavailable_comparisons':[],'after_all_fields_totals':{'regions':0,'ui_proposed':0},'totals':{'before_regions':0,'after_regions':0,'before_selected_useful':0,'after_selected_useful':0,'before_ui_proposed':0,'after_ui_proposed':0}}
    for tile,entry in new.items():
        if entry['state']!='assessed':continue
        nf,b=verified_payload(after,entry)
        is_selected=lambda r:r['selected_for_review'] and any(q.get('name')=='useful_size' and q.get('status')=='pass' for q in r['sieve_results'])
        output['after_all_fields_totals']['regions']+=len(b['regions'])
        output['after_all_fields_totals']['ui_proposed']+=sum(is_selected(r) for r in b['regions'])
        if old.get(tile,{}).get('state')!='assessed':continue
        of=before/old[tile]['field_json']
        try:of,a=verified_payload(before,old[tile])
        except FileNotFoundError:
            output['unavailable_comparisons'].append({'tile':tile,'reason':'original_field_shard_missing'});continue
        if a['source_hashes']!=b['source_hashes']:raise ValueError('Different acquisition inputs')
        counts={'before_regions':len(a['regions']),'after_regions':len(b['regions']),
            'before_selected_useful':sum(r['selected_for_review'] and r['area_um2']>=25 for r in a['regions']),
            'after_selected_useful':sum(r['selected_for_review'] and r['area_um2']>=25 for r in b['regions']),
            'before_ui_proposed':sum(r['selected_for_review'] and any(q.get('name')=='useful_size' and q.get('status')=='pass' for q in r['sieve_results']) for r in a['regions']),
            'after_ui_proposed':sum(r['selected_for_review'] and any(q.get('name')=='useful_size' and q.get('status')=='pass' for q in r['sieve_results']) for r in b['regions'])}
        output['fields'][tile]=counts
        for key,value in counts.items():output['totals'][key]+=value
        if tile not in bytile and not any(is_selected(r) for r in b['regions']):continue
        import io
        try:
            old_blob=driver.retry_read(of.with_name('masks.npz').read_bytes);new_blob=driver.retry_read(nf.with_name('masks.npz').read_bytes)
        except FileNotFoundError:
            output['unavailable_comparisons'].append({'tile':tile,'reason':'mask_missing_for_overlap'});continue
        for folder,record,path,blob in [(before,old[tile],of.with_name('masks.npz'),old_blob),(after,entry,nf.with_name('masks.npz'),new_blob)]:
            if hashlib.sha256(blob).hexdigest()!=record['hashes'][path.relative_to(folder).as_posix()]:raise ValueError('Changed masks before overlap comparison')
        with np.load(io.BytesIO(old_blob),allow_pickle=False) as archive:ol=archive['labels']
        with np.load(io.BytesIO(new_blob),allow_pickle=False) as archive:nl=archive['labels']
        lookup={r['instance_label']:r for r in b['regions']};oldlookup={r['instance_label']:r for r in a['regions']}
        sizes=np.bincount(nl.ravel())
        for region in b['regions']:
            if not is_selected(region):continue
            mask=nl==region['instance_label'];overlap=np.bincount(ol[mask]);overlap[0]=0;best=int(np.argmax(overlap));previous=oldlookup.get(best,{})
            fraction=float(overlap[best]/max(1,int(mask.sum())))
            if previous and not is_selected(previous) and fraction>=.5:
                output['promotions'].append({'new_id':'chip001/'+tile+'/'+str(region['instance_label']),'original_id':'chip001/'+tile+'/'+str(best),
                    'new_mask_fraction_overlapping_original':fraction,'area_um2':region['area_um2'],'contrast_rgb':region['contrast_rgb'],'previous_filter_reasons':previous['filter_reasons']})
        for sample in bytile.get(tile,[]):
            ident=int(sample['id'].split('/')[-1]);mask=ol==ident;overlap=np.bincount(nl[mask],minlength=len(sizes));overlap[0]=0
            best=int(np.argmax(overlap));newrow=lookup.get(best,{});oldrow=oldlookup.get(ident,{})
            intersection=int(overlap[best]);union=int(mask.sum()+sizes[best]-intersection)
            output['observations'].append({'original_id':sample['id'],'operator_negative_band':sample['id'] in negative,
                'new_id':sample['id'].rsplit('/',1)[0]+'/'+str(best),'intersection_over_union':intersection/max(1,union),
                'original_mask_recovered_fraction':intersection/max(1,int(mask.sum())),
                'before_selected':oldrow.get('selected_for_review'),'after_selected':newrow.get('selected_for_review'),
                'before_ui_proposed':bool(oldrow.get('selected_for_review') and any(q.get('name')=='useful_size' and q.get('status')=='pass' for q in oldrow.get('sieve_results',[]))),
                'after_ui_proposed':bool(newrow.get('selected_for_review') and any(q.get('name')=='useful_size' and q.get('status')=='pass' for q in newrow.get('sieve_results',[]))),
                'before_area_um2':oldrow.get('area_um2'),'after_area_um2':newrow.get('area_um2'),
                'after_class':newrow.get('class_name'),'after_contrast_rgb':newrow.get('contrast_rgb'),
                'after_filter_reasons':newrow.get('filter_reasons')})
    output['scope']='Before/after shared readable field counts use saved UI disposition rules, before artifact hiding and dedup. Promotions require >=50% new-mask overlap with a previously uncertain observation; neither object identity nor ground truth is asserted.'
    output['matched_promotions_count']=len(output['promotions'])
    return output

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--before',type=Path,required=True);p.add_argument('--after',type=Path,required=True)
    p.add_argument('--observations',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=compare(a.before,a.after,a.observations);a.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({'totals':result['totals'],'after_all_fields_totals':result['after_all_fields_totals'],'matched_promotions_count':result['matched_promotions_count'],'unavailable_comparisons':result['unavailable_comparisons']}))
