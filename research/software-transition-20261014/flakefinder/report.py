from pathlib import Path
import html,json

def write_report(folder):
    from .storage import managed_output_path
    folder=Path(folder)
    output=managed_output_path(folder)
    output.mkdir(parents=True,exist_ok=True)
    for review in [output/'analysis/review.html',output/'review.html']:
        if review.exists():return review
    catalog=output/'flakes.json' if (output/'flakes.json').exists() else folder/'flakes.json'
    flakes=json.loads(catalog.read_text())['flakes'] if catalog.exists() else []
    rows=[]
    for f in flakes:
        chip=f.get('chip_id') or ''
        image=(Path(chip)/'tiles'/f"{f['tile_id']}.jpg") if (folder/chip/'tiles').exists() else Path('tiles')/f"{f['tile_id']}.jpg"
        xy=f.get('stage_xy_um') or [0,0]
        rows.append('<tr><td>'+html.escape(f['id'])+'</td><td>'+html.escape(f['label'])+
            f'</td><td>{f["area_um2"]:.1f}</td><td>{xy[0]:.2f}</td><td>{xy[1]:.2f}</td>'+
            '<td>'+', '.join(f'{v:.4f}' for v in f['contrast_rgb'])+'</td><td><a href="'+html.escape(image.as_posix(),quote=True)+'">Tile image</a></td></tr>')
    images=''.join(f'<figure><img src="{name}" alt="{name}"><figcaption>{name}</figcaption></figure>' for name in ['chips.png','overview.jpg'] if (folder/name).exists())
    text='''<!doctype html><html lang="en"><meta charset="utf-8"><title>FlakeFinder results</title>
<style>body{font:15px system-ui;margin:40px;color:#172638;background:#f5f7fa}table{border-collapse:collapse;background:white;width:100%}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left}img{max-width:850px;max-height:600px}h1{font-size:30px}a{color:#06758a}</style>
<h1>FlakeFinder results</h1><p>Optical flake proposals. Layer labels require a calibrated reference model and independent confirmation. Coordinates are stage micrometres.</p>'''+images+f'<p>{len(flakes)} candidates. Raw RGB TIFFs, metadata, masks, focus curves, and checkpoints accompany this report.</p><table><thead><tr><th>ID</th><th>Label</th><th>Area µm²</th><th>X µm</th><th>Y µm</th><th>RGB contrast</th><th>Image</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></html>'
    (output/'report.html').write_text(text,encoding='utf-8')
    return output/'report.html'
