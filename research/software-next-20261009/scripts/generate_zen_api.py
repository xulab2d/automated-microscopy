"""Generate bindings from the API contracts installed with this microscope."""
from pathlib import Path
import argparse
import grpc_tools
from grpc_tools import protoc

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--proto-root', default=r'C:\Users\Public\Documents\Carl Zeiss\ZEN API Proto Files')
args = parser.parse_args()
source = Path(args.proto_root)
files = sorted(source.rglob('*.proto'))
if not files:
    raise SystemExit('No ZEISS API contracts found; install the ZEN API Proto Files component.')
dest = root / 'generated'
dest.mkdir(exist_ok=True)
result = protoc.main(['protoc', '-I' + str(source), '-I' + str(Path(grpc_tools.__file__).parent / '_proto'),
    '--python_out=' + str(dest), '--grpc_python_out=' + str(dest)] + [str(f) for f in files])
if result:
    raise SystemExit(result)
print(f'Generated {len(files)} ZEISS API contracts in {dest}')
