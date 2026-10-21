"""Offline primary-data retrieval; no microscope connection."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, requests

ROOT = Path(__file__).resolve().parent
SOURCES = {
    'MoTe2_Munkhbat_tensor_nm.txt': 'https://zenodo.org/records/6205431/files/f3_dataset_MoTe2_nm_ex1_ex2_ey1_ey2_ez1_ez2.txt',
    'Munkhbat_zenodo_metadata.json': 'https://zenodo.org/api/records/6205431',
    'Munkhbat_article.pdf': 'https://research.chalmers.se/publication/531148/file/531148_Fulltext.pdf',
    'synthetic_generator_tree.json': 'https://api.github.com/repos/Jaluus/MaskTerial-Synthetic-Data/git/trees/main?recursive=1',
    'Malitson_fused_silica.yml': 'https://raw.githubusercontent.com/polyanskiy/refractiveindex.info-database/master/database/data/main/SiO2/nk/Malitson.yml',
}

def main():
    records = []
    for name, url in SOURCES.items():
        path = ROOT / name
        row = {'file': name, 'url': url}
        try:
            if not path.exists():
                response = requests.get(url, timeout=35)
                response.raise_for_status()
                path.write_bytes(response.content)
            blob = path.read_bytes()
            row.update(bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(), md5=hashlib.md5(blob).hexdigest(), status='retrieved')
        except Exception as exc:
            row.update(status='failed', error=str(exc))
        records.append(row)
        print(name, row['status'], row.get('bytes', row.get('error')))
    (ROOT / 'retrieval.json').write_text(json.dumps({'created_utc': datetime.now(timezone.utc).isoformat(), 'hardware_access': False, 'sources': records}, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
