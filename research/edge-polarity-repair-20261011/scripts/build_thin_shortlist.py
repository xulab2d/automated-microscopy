"""Build a read-only cross-chip near-match page from saved review assets."""
import argparse
from html import escape
import json
import math
from pathlib import Path
from urllib.parse import quote, urlencode


def build_shortlist(batch_root, limit=12):
    root=Path(batch_root).resolve()
    rows=[];source_hashes={}
    sources=sorted(root.glob('*/analysis-thin-contrast/review-data.json'))
    for source in sources:
        data=json.loads(source.read_text(encoding='utf-8'))
        for tile in data.get('summary',{}).get('tiles',[]):
            source_hashes[(source.parent,tile['key'])]=tile.get('sha256')
        for row in data.get('flakes',[]):
            contrast=row.get('material_selection',{}).get('contrast_rgb',row.get('contrast_rgb',[]))
            evidence=row.get('thin_screen',{})
            rz=evidence.get('red_z');grz=evidence.get('green_minus_red_z')
            if not (len(contrast)==3 and all(isinstance(v,(float,int)) and math.isfinite(v) for v in contrast)):
                continue
            if not (isinstance(rz,(float,int)) and isinstance(grz,(float,int)) and math.isfinite(rz) and math.isfinite(grz)):
                continue
            if row.get('area_um2',0)<100 or row.get('usable_width_um',0)<5 or not (.02<=contrast[1]<=.45 and 0<contrast[0]<contrast[1]):
                continue
            rows.append((min(rz,grz),source.parent,row,contrast))
    rows.sort(key=lambda item:(-item[0],-item[2]['area_um2'],item[2]['id']))
    feedback_path=root/'thin-shortlist-feedback.json'
    feedback=json.loads(feedback_path.read_text(encoding='utf-8')) if feedback_path.is_file() else {}
    annotations={a['id']:a for a in feedback.get('annotations',[])}
    def confirmed(item):
        _,folder,row,_=item;a=annotations.get(row['id'],{})
        return (a.get('status')=='human_confirmed' and a.get('label')=='bare_substrate'
                and a.get('tile_key')==row['tile_key']
                and a.get('source_image_sha256')==source_hashes.get((folder,row['tile_key']))
                and a.get('polygon_px')==row['polygon_px']
                and a.get('holes_px',[])==row.get('holes_px',[]))
    if annotations:
        # Keep the human-reviewed ranking; never replace rejected controls with
        # the next unreviewed objects and present those as new discoveries.
        by_id={item[2]['id']:item for item in rows}
        displayed=[(a['original_rank'],by_id[a['id']]) for a in sorted(annotations.values(),key=lambda a:a['original_rank']) if a['id'] in by_id]
    else:displayed=list(enumerate(rows[:limit],1))
    confirmed_count=sum(confirmed(item) for _,item in displayed)

    def asset(folder,name):
        path=(folder/name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f'Missing or unsafe review asset: {path}')
        return quote(path.relative_to(root).as_posix(),safe='/')

    cards=[]
    for rank,item in displayed:
        score,folder,row,contrast=item
        crop=asset(folder,row['crop_image']);full=asset(folder,row['tile_image'])
        review=asset(folder,'review.html')+'?'+urlencode({'candidate':row['id'],'outline':'1'})
        x0,y0,x1,y1=map(float,row['crop_bounds_px']);height,width=row['image_shape']
        sensor_x,sensor_y=map(float,row['centroid_px']);screen=row['thin_screen']
        polygons=[row['polygon_px']]+row.get('holes_px',[])
        paths=[]
        for polygon in polygons:
            points=[]
            for x,y in polygon:
                if not math.isfinite(float(x)) or not math.isfinite(float(y)):
                    raise ValueError('Non-finite outline coordinate.')
                points.append(f'{float(x)},{float(y)}')
            paths.append('<polygon points="'+escape(' '.join(points),quote=True)+'"/>')
        tile=escape(str(row.get('tile_key',row.get('tile_id',''))))
        status='<div class="feedback">Confirmed bare substrate</div>' if confirmed(item) else ''
        cards.append(f'''<article class="card">
<div class="cardhead"><strong>#{rank} · {tile}</strong><span class="score">z {score:.2f} / 3</span></div>
{status}
<div class="picture" style="aspect-ratio:{x1-x0}/{y1-y0}"><img src="{crop}" alt="Raw display crop, {tile}" loading="lazy"><svg class="outline" viewBox="{x0} {y0} {x1-x0} {y1-y0}" aria-hidden="true">{''.join(paths)}</svg></div>
<div class="details"><div class="metrics"><span>Area <b>{row['area_um2']:.1f} µm²</b></span><span>Width <b>{row['usable_width_um']:.1f} µm</b></span><span>R z <b>{screen['red_z']:.2f}</b></span><span>(G−R) z <b>{screen['green_minus_red_z']:.2f}</b></span></div>
<div class="sensor"><svg viewBox="0 0 62 52" aria-label="Position in full sensor frame"><rect x="1" y="1" width="60" height="50"/><circle cx="{1+sensor_x/width*60:.2f}" cy="{1+sensor_y/height*50:.2f}" r="3"/></svg><span>Sensor x {sensor_x:.0f}, y {sensor_y:.0f}<small>Full frame {width} × {height} px · origin top left</small></span></div>
<p class="contrast">Local contrast R {contrast[0]:.4f} · G {contrast[1]:.4f} · B {contrast[2]:.4f}</p>
<div class="actions"><button class="toggle" aria-pressed="false">Show outline</button><a href="{full}" target="_blank" rel="noopener">Full source image</a><a href="{escape(review,quote=True)}">Open review</a></div></div></article>''')
    recurring=[item for item in rows if item[2]['centroid_px'][0]<160 and item[2]['centroid_px'][1]>1820]
    chip_count=len({item[1].parent.name for item in recurring})
    pattern=(f'All {len(rows)} point-color matches recur in the lower-left sensor region (x &lt; 160, y &gt; 1820) across {chip_count} chips. '
             if rows and len(recurring)==len(rows) else f'{len(recurring)} of {len(rows)} point-color matches fall in the same lower-left sensor region across {chip_count} chips. ')
    pattern+='The repeated circular pattern is likely an optical artifact, not physical flakes.' if len(recurring)>1 and chip_count>1 else ''
    best=f'{rows[0][0]:.2f}' if rows else '—'
    document='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Closest contrast matches</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b121b;color:#edf3f8;font:14px 'Segoe UI',system-ui,sans-serif}header,main{max-width:1480px;margin:auto;padding:20px}h1{font-size:26px;margin:0 0 8px}p{line-height:1.5;color:#b6c7d4}.notice{background:#30261a;border:1px solid #856642;padding:12px 15px;border-radius:8px;color:#f2d3a8}.toolbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;font-size:13px}.toolbar input{accent-color:#6ce1bc}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.card{background:#121e2a;border:1px solid #314455;border-radius:9px;overflow:hidden}.cardhead{display:flex;justify-content:space-between;gap:10px;padding:12px;font-size:12px}.cardhead strong{overflow-wrap:anywhere}.score{white-space:nowrap;color:#f2d3a8}.picture{position:relative;background:#03080d}.picture img,.outline{display:block;width:100%;height:100%;object-fit:contain}.outline{position:absolute;inset:0;display:none;pointer-events:none}.outlined .outline{display:block}.outline polygon{fill:none;stroke:#ff5bc2;stroke-width:1.8;vector-effect:non-scaling-stroke}.details{padding:12px}.metrics{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:#a6b8c6}.metrics b{display:block;color:#edf3f8;font-size:13px;margin-top:3px}.sensor{display:flex;align-items:center;gap:10px;margin-top:12px;color:#d6e3ec;font-size:12px}.sensor svg{width:50px;height:42px}.sensor rect{stroke:#5b7286;fill:#08121c}.sensor circle{fill:#ffcb75}.sensor small{display:block;color:#9db0c0;font-size:10px;margin-top:4px}.contrast{font-size:11px;margin:10px 0}.actions{display:flex;align-items:center;gap:13px;flex-wrap:wrap;font-size:12px}button{cursor:pointer;color:#d6fff2;background:#183b34;border:1px solid #3e7767;border-radius:5px;padding:7px 9px}a{color:#77dec4}footer{max-width:1480px;margin:auto;padding:0 20px 24px;font-size:12px;color:#9bafbf}@media(max-width:1050px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:650px){.grid{grid-template-columns:1fr}h1{font-size:22px}}
</style><header><h1>Closest contrast matches — not accepted flakes</h1>'''
    if confirmed_count:
        document=document.replace('<title>Closest contrast matches</title>','<title>Rejected substrate controls</title>').replace('Closest contrast matches — not accepted flakes','Rejected substrate controls')
        document=document.replace('</style>','.feedback{padding:9px 12px;background:#453222;color:#ffd0a7;font-weight:600;border-top:1px solid #715437}</style>')
        document+=f'<p>{confirmed_count} displayed regions confirmed as bare substrate by the user. The original ranking and boundaries are retained for audit; these are not active flake candidates.</p>'
        document+=f'<div class="notice">User: “{escape(feedback.get("user_quote",""))}”. Confirmation applies only to these {confirmed_count} displayed regions. The other {len(rows)-confirmed_count} similar matches remain unreviewed and occur at similar fixed sensor positions.</div>'
        document+='<p>Original contrast ranking retained below. Rejected controls are not replaced by the next unreviewed matches.</p>'
    else:
        document+=f'<p>Top {len(displayed)} of {len(rows)} saved point-color matches · best weaker-direction z {best}; required z ≥ 3. No material identity or thickness is assigned.</p><div class="notice">{pattern}</div>'
        document+='<p>Ranked by min(R z, (G−R) z), with area ≥ 100 µm², usable width ≥ 5 µm, 0.02 ≤ G ≤ 0.45, and 0 &lt; R &lt; G. These are near misses for inspection, not qualified candidates.</p>'
    document+='<div class="toolbar"><label><input id="allOutlines" type="checkbox"> Show all outlines</label><span>Raw crops by default · click a card’s toggle to compare its saved boundary</span><a href="/">Back to batch</a></div></header><main class="grid">'
    document+=''.join(cards) if cards else '<p>No saved objects meet the stated point-color and size filters.</p>'
    document+='''</main><footer>Saved raw-image display previews and stored source-pixel polygons. This page neither captures images nor moves the microscope.</footer><script>
'use strict';
function setOutline(card,visible){card.classList.toggle('outlined',visible);const button=card.querySelector('.toggle');button.textContent=visible?'Hide outline':'Show outline';button.setAttribute('aria-pressed',String(visible))}
document.querySelectorAll('.toggle').forEach(button=>button.onclick=()=>{const card=button.closest('.card');setOutline(card,!card.classList.contains('outlined'))});
document.getElementById('allOutlines').onchange=e=>document.querySelectorAll('.card').forEach(card=>setOutline(card,e.target.checked));
</script></html>'''
    output=root/'thin-shortlist.html';temporary=output.with_suffix('.tmp.html')
    temporary.write_text(document,encoding='utf-8');temporary.replace(output)
    return {'output':str(output),'source_analyses':len(sources),'matches':len(rows),'shown':len(displayed),
            'human_confirmed_bare_substrate':confirmed_count,'unreviewed_point_matches':len(rows)-confirmed_count,
            'best_z':rows[0][0] if rows else None,'sensor_corner_matches':len(recurring),'corner_chips':chip_count}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('batch_root',nargs='?',default='data/chip-batch-01');parser.add_argument('--limit',type=int,default=12)
    args=parser.parse_args();print(json.dumps(build_shortlist(args.batch_root,args.limit),indent=2))
