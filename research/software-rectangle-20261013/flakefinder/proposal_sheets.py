"""Ranked raw crops and separate outline sheets for manual optical review."""
from pathlib import Path
import json
import math

from PIL import Image,ImageDraw,ImageFont

from .model import atomic_json


def build_proposal_sheets(folder,per_sheet=12,max_rows=144):
    folder=Path(folder).resolve()
    data=json.loads((folder/'review-data.json').read_text(encoding='utf-8'))
    bulk=data.get('summary',{}).get('backend')=='bulk_contrast_reference'
    rows=[r for r in data['flakes'] if r.get('disposition')=='candidate'
          and ('material_selection' not in r or r['material_selection'].get('catalog_selected',False))]
    rows.sort(key=lambda row:(-row.get('thin_screen',{}).get('score',0),-row.get('thin_screen_area_um2',0),-row['area_um2']))
    chosen=rows[:max_rows]
    destination=folder/'contact-sheets';destination.mkdir(exist_ok=True)
    try:font=ImageFont.truetype('arial.ttf',15);small=ImageFont.truetype('arial.ttf',13)
    except OSError:font=ImageFont.load_default();small=font
    columns=4;cell_w=320;image_h=260;cell_h=350
    sheets=[];index=[]
    for page in range(max(1,math.ceil(len(chosen)/per_sheet))):
        subset=chosen[page*per_sheet:(page+1)*per_sheet]
        canvas_h=62+math.ceil(max(1,len(subset))/columns)*cell_h
        raw=Image.new('RGB',(columns*cell_w,canvas_h),(19,23,29));outlined=raw.copy()
        for kind,canvas in (('raw crops',raw),('outline comparison',outlined)):
            heading=(f'Bulk positive controls: {len(rows)} size-qualified catalog objects | {kind} | NOT THIN TARGETS' if bulk else
                     f'Empirical optical proposals: {len(rows)} total | {kind} | material/layer unconfirmed')
            ImageDraw.Draw(canvas).text((12,8),heading,fill='white',font=font)
            ImageDraw.Draw(canvas).text((12,31),('All per-field detections, including smaller objects, remain in the tile overlays. Raw TIFFs are unchanged.' if bulk else 'Rank: quality-supported thin score, then optical area. Color-only matches are not thin candidates.'),fill=(195,204,217),font=small)
        if not subset:
            message=('No size-qualified bulk reference objects. Smaller detections remain in tile overlays.' if bulk else
                     'No useful-size empirical proposals in this analysis. This is not evidence of absence.')
            for canvas in (raw,outlined):ImageDraw.Draw(canvas).text((20,110),message,fill='white',font=font)
        for j,row in enumerate(subset):
            rank=page*per_sheet+j+1;left=(j%columns)*cell_w;top=62+(j//columns)*cell_h
            crop=Image.open(folder/row['crop_image']).convert('RGB')
            crop.thumbnail((cell_w-12,image_h-8));ix=left+(cell_w-crop.width)//2;iy=top+(image_h-crop.height)//2
            raw.paste(crop,(ix,iy));outlined.paste(crop,(ix,iy))
            x0,y0,x1,y1=row['crop_bounds_px'];sx=crop.width/(x1-x0);sy=crop.height/(y1-y0)
            draw=ImageDraw.Draw(outlined)
            def polygon(points,color):
                values=[(ix+(x-x0)*sx,iy+(y-y0)*sy) for x,y in points]
                if len(values)>=3:draw.line(values+[values[0]],fill=color,width=2)
            polygon(row['polygon_px'],(255,207,82))
            for terrace in row.get('terraces',[]):
                if terrace.get('disposition')=='candidate' and terrace.get('thin_screen',{}).get('score',0)>0:
                    polygon(terrace['polygon_px'],(83,220,255))
            green=(row.get('thin_screen_contrast_rgb') or row['contrast_rgb'])[1];score=row.get('thin_screen',{}).get('score',0)
            texts=[f'{rank:03d}  {row["tile_key"]}  {row["id"][-6:]}',
                   f'Parent {row["area_um2"]:.0f} um2 | thin-screen {row.get("thin_screen_area_um2",0):.0f} um2',
                   f'Screen G {green:+.3f} | score {score:.2f} | width {row["usable_width_um"]:.1f} um',
                   (('FOCUS LIMITED | ' if row.get('focus_limited') else '')+', '.join(row.get('evidence_warnings',[])))[:46]]
            if bulk:
                texts[1]=f'Area {row["area_um2"]:.0f} um2 | bulk positive control'
                texts[2]=f'R/G contrast {row["contrast_rgb"][0]:+.3f}/{row["contrast_rgb"][1]:+.3f}'
            elif row.get('thin_screen',{}).get('status')=='color_match_only':
                texts[1]=f'Parent {row["area_um2"]:.0f} um2 | color match only'
                texts[2]=f'Color G {green:+.3f} | compatibility {row.get("color_compatibility",{}).get("score",0):.2f} | quality failed'
            for canvas in (raw,outlined):
                painter=ImageDraw.Draw(canvas)
                for line,text in enumerate(texts):painter.text((left+6,top+image_h+3+line*19),text,fill=(218,226,235),font=small)
            index.append({'rank':rank,'id':row['id'],'tile_key':row['tile_key'],'source_image':row['source_image'],
                          'crop_image':row['crop_image'],'crop_bounds_px':row['crop_bounds_px'],
                          'area_um2':row['area_um2'],'thin_screen':row.get('thin_screen'),
                          'sheet':page+1,'cell':j+1,'stage_xy_um':row.get('stage_xy_um')})
        paths=[]
        for name,canvas in (('raw',raw),('outlines',outlined)):
            path=destination/f'{name}-{page+1:03d}.jpg';canvas.save(path,quality=95)
            paths.append(path.relative_to(folder).as_posix())
        sheets.append({'raw':paths[0],'outlines':paths[1],'first_rank':page*per_sheet+1,'count':len(subset)})
    result={'schema':1,'dataset_id':data['dataset_id'],'candidate_count':len(rows),'displayed_count':len(chosen),
            'ranking':'quality-supported thin score, qualified thin-screen area, optical region area','sheets':sheets,'index':index,
            'warning':'Empirical optical proposals, not confirmed flakes or layer assignments. Raw sheets contain no segmentation overlays.'}
    if bulk:
        result['ranking']='size-qualified bulk-reference area'
        result['warning']='User-selected bulk optical population, not useful thin targets. Catalog counts exclude smaller source observations; raw sheets contain no overlays.'
    atomic_json(destination/'index.json',result)
    return {key:result[key] for key in ('candidate_count','displayed_count','sheets')}
