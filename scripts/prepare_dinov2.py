"""Download a pinned public DINOv2 model into the shared project, without login."""
from pathlib import Path
import hashlib, json, sys, urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flakefinder.storage import resolve_output_path
from flakefinder.model import atomic_json

REVISION='ed25f3a31f01632728cabb09d1542f84ab7b0056'


def main():
    root=resolve_output_path(Path(__file__).resolve().parents[1]/'external/dinov2-small'/REVISION)
    root.mkdir(parents=True,exist_ok=True)
    receipt=root/'download-provenance.json'
    previous=json.loads(receipt.read_text()) if receipt.exists() else {'files':{}}
    records={}
    for name in ('config.json','preprocessor_config.json','model.safetensors','README.md'):
        target=root/name
        if target.exists():
            digest=hashlib.sha256(target.read_bytes()).hexdigest()
            if previous['files'].get(name,{}).get('sha256')!=digest:
                raise RuntimeError('Existing model file has no matching download receipt: '+name)
            records[name]=previous['files'][name];continue
        url=f'https://huggingface.co/facebook/dinov2-small/resolve/{REVISION}/{name}'
        part=root/(name+'.part');digest=hashlib.sha256();size=0
        with urllib.request.urlopen(url,timeout=60) as response, part.open('xb') as stream:
            expected=int(response.headers['Content-Length']) if response.headers.get('Content-Length') else None
            while block:=response.read(1024*1024):
                stream.write(block);digest.update(block);size+=len(block)
        if expected is not None and size!=expected:raise RuntimeError('Incomplete download: '+name)
        part.replace(target)
        records[name]={'source':url,'sha256':digest.hexdigest(),'bytes':size}
        atomic_json(receipt,{'model':'facebook/dinov2-small','revision':REVISION,'files':records,'license':'Apache-2.0'})
        print(json.dumps({'downloaded':name,'bytes':size}),flush=True)
    print(root,flush=True)


if __name__=='__main__':main()
