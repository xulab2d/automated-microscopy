"""Focused relocation and read-only route checks; stdlib unittest only."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import gzip
import http.client
import importlib.util
import json
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_laptop_viewer import export


def load_launcher():
    spec = importlib.util.spec_from_file_location('portable_launcher', ROOT/'laptop-viewer/Launcher.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


class PortableViewerTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name)/'before'
        self.data = self.base/'data/campaign'
        self.package = self.base/'laptop-viewer'
        self.package.mkdir(parents=True)
        shutil.copyfile(ROOT/'laptop-viewer/Launcher.py', self.package/'Launcher.py')
        folder = self.data/'chip1';analysis = folder/'analysis-staged-features'
        image = folder/'tiles/tile_000000.jpg';image.parent.mkdir(parents=True)
        image.write_bytes(b'preview')
        (image.parent/'tile_000001.jpg').write_bytes(b'pending preview')
        save(folder/'manifest.json', {'context':{'camera':'one','exposure_ms':200},
            'completed':{'tile_000000':{'position':{'x':1,'y':2,'z':3}},'tile_000001':{}}})
        save(analysis/'analysis.json', {'tiles':[{'tile_id':'tile_000000','observations_file':'fields/one.json'}]})
        rows = [
            {'id':'selected','selected_for_review':True,'area_um2':150,'usable_width_um':8,
             'source_image':str(image.with_suffix('.tiff')),'polygon_px':[[0,0],[10,0],[10,10]],
             'local_contrast_measurement':{'measurement_status':'measured','contrast_rgb':[.1,.2,.3]}},
            {'id':'rejected','selected_for_review':False,'area_um2':40,'usable_width_um':4,
             'local_contrast_measurement':{'measurement_status':'measured','contrast_rgb':[-27,.4,9]}},
            {'id':'unmeasured','selected_for_review':False,'contrast_rgb':[900,900,900]}]
        save(analysis/'fields/one.json', {'rows':rows,'tile':{'shape':[100,100]},'provenance':{}})

    def tearDown(self):
        self.temp.cleanup()

    def test_export_and_relocated_routes_without_scientific_dependencies(self):
        summary = export(self.data,self.package)
        self.assertEqual(summary['image_count'],2)
        self.assertEqual(summary['counts']['measured'],2)
        self.assertEqual(summary['counts']['unmeasured'],1)
        manifest = json.loads((self.package/'snapshot.json').read_text())
        field = json.loads(gzip.decompress((self.package/manifest['field_files']['chip1/tile_000000']).read_bytes()))
        self.assertEqual(field['rows'][0]['source_image'],'chip1/tiles/tile_000000.tiff')
        self.assertEqual(field['rows'][0]['polygon_px'],[[0,0],[10,0],[10,10]])
        plot = json.loads(gzip.decompress((self.package/'snapshots/contrast.json.gz').read_bytes()))
        self.assertEqual(plot['points'][1]['c'],[-27,.4,9])
        self.assertEqual(plot['field_statuses'],{'ready':1,'pending':1})
        relocated = self.base.with_name('after')
        self.base.rename(relocated)
        server,url,data = load_launcher().serve(relocated/'laptop-viewer')
        worker = Thread(target=server.serve_forever,daemon=True);worker.start()
        def request(path,method='GET',headers=None):
            connection = http.client.HTTPConnection('127.0.0.1',server.server_port)
            connection.request(method,path,headers=headers or {})
            response=connection.getresponse();body=response.read();connection.close()
            return response.status,body,response.getheader('Content-Encoding')
        try:
            self.assertEqual(data,relocated/'data/campaign')
            self.assertEqual(request('/')[0],200)
            self.assertEqual(request('/gallery?chip=chip1&tile=tile_000000&feature=selected')[0],200)
            self.assertEqual(json.loads(request('/api/gallery-field?chip=chip1&tile=tile_000000')[1])['rows'][0]['id'],'selected')
            self.assertEqual(request('/files/chip1/tiles/tile_000000.jpg')[1],b'preview')
            self.assertEqual(request('/api/gallery',headers={'Accept-Encoding':'gzip'})[2],'gzip')
            self.assertEqual(request('/api/gallery',headers={'Host':'external.example'})[0],403)
            self.assertEqual(request('/files/../../laptop-viewer/Launcher.py')[0],404)
            self.assertEqual(request('/files/chip1/manifest.json')[0],404)
            self.assertEqual(request('/api/stop',method='POST')[0],405)
            self.assertTrue(json.loads(request('/api/contrast-distribution?since='+manifest['contrast_signature'])[1])['unchanged'])
        finally:
            server.shutdown();server.server_close();worker.join()

    def test_changed_snapshot_fails_closed(self):
        export(self.data,self.package)
        (self.package/'gallery.html').write_text('changed')
        server,_,_ = load_launcher().serve(self.package)
        worker = Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1',server.server_port)
            connection.request('GET','/gallery')
            response=connection.getresponse();response.read();connection.close()
            self.assertEqual(response.status,503)
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__ == '__main__':
    unittest.main()
