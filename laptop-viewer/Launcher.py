"""Read-only portable saved-microscopy viewer. Python 3.9+ standard library only."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
import argparse
import gzip
import hashlib
import json
import mimetypes
import webbrowser

HERE = Path(__file__).resolve().parent


def inside(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        return None
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or ':' in relative:
        return None
    target = (root / path).resolve()
    return target if target.is_relative_to(root) else None


def serve(package=HERE, data=None, port=0):
    package = Path(package).resolve()
    manifest = json.loads((package/'snapshot.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('kind') != 'portable_microscopy_viewer':
        raise ValueError('Unsupported or unfinished viewer snapshot')
    data = Path(data).resolve() if data else (package/manifest['data_relative']).resolve()
    # Snapshot JSON is the only API input. No microscope software is imported.
    encoded = {}
    for name, digest in manifest['artifacts'].items():
        target = inside(package, name)
        if target is None or not target.is_file():
            raise ValueError('Missing or escaped snapshot asset: '+name)
        if name.endswith('.gz') or name.endswith('.html'):
            # Validate on demand, keeping startup quick with 1,000+ fields.
            encoded[name] = (target, digest)
    fields = manifest['field_files']
    assets = manifest['data_assets']

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, kind='application/json; charset=utf-8', encoding=None):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            if encoding:
                self.send_header('Content-Encoding', encoding)
            self.end_headers()
            self.wfile.write(body)

        def asset(self, name, kind='application/json; charset=utf-8'):
            try:
                target, digest = encoded[name]
                body = target.read_bytes()
                if hashlib.sha256(body).hexdigest() != digest:
                    raise ValueError('Snapshot checksum mismatch')
                compression = None
                if name.endswith('.gz'):
                    if 'gzip' in self.headers.get('Accept-Encoding', ''):
                        compression = 'gzip'
                    else:
                        body = gzip.decompress(body)
                return self.reply(200, body, kind, compression)
            except (OSError, KeyError, ValueError):
                return self.reply(503, b'{"error":"Saved viewer asset missing or changed; re-export the snapshot."}')

        def do_GET(self):
            if self.headers.get('Host') != '127.0.0.1:'+str(self.server.server_port):
                return self.reply(403, b'{}')
            split = urlsplit(self.path)
            path, query = unquote(split.path), parse_qs(split.query)
            pages = {'/':'index.html','/gallery':'gallery.html','/gallery.html':'gallery.html',
                     '/contrast':'contrast.html','/contrast.html':'contrast.html'}
            if path in pages:
                return self.asset(pages[path], 'text/html; charset=utf-8')
            if path == '/api/gallery':
                return self.asset('snapshots/gallery.json.gz')
            if path == '/api/contrast-distribution':
                if query.get('since') == [manifest['contrast_signature']]:
                    return self.reply(200, json.dumps({'unchanged':True,'signature':manifest['contrast_signature']}).encode())
                return self.asset('snapshots/contrast.json.gz')
            if path == '/api/gallery-field':
                key = query.get('chip', [''])[0]+'/'+query.get('tile', [''])[0]
                if key in fields:
                    return self.asset(fields[key])
                return self.reply(404, b'{"error":"Unknown saved field."}')
            if path.startswith('/files/'):
                relative = path[len('/files/'):]
                target = inside(data, relative)
                if relative not in assets or target is None or not target.is_file():
                    return self.reply(404, b'{"error":"Saved preview missing; download the accompanying data folder or use --data."}')
                if target.stat().st_size != assets[relative]['bytes']:
                    return self.reply(409, b'{"error":"Saved preview differs from this snapshot."}')
                return self.reply(200, target.read_bytes(), mimetypes.guess_type(relative)[0] or 'application/octet-stream')
            return self.reply(404, b'{}')

        def do_POST(self):
            return self.reply(405, b'{"error":"Offline viewer is read-only."}')

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server, 'http://127.0.0.1:'+str(server.server_port)+'/', data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, help='Dataset folder when not alongside the original data/ directory')
    parser.add_argument('--port', type=int, default=0, help='Default: choose an available local port')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server, url, data = serve(data=args.data, port=args.port)
    print('Offline microscopy viewer: '+url, flush=True)
    print('Saved images: '+str(data), flush=True)
    print('Read-only; no microscope connection. Close this window or press Ctrl+C to stop.', flush=True)
    if not data.is_dir():
        print('WARNING: data folder is missing. Download it from Drive or use --data.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
