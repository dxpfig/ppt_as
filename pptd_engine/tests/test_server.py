"""HTTP boundaries: no silent font fallback and malformed exports return JSON."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('pptd_server',ROOT/'experiments/pptd-v2/server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        source=self.root/'source';source.mkdir()
        (source/'main.pptd').write_text('version: v2\nsize: [960,540]\npages: [one.page]\n')
        (source/'one.page').write_text('elements:\n- elementId: text\n  elementType: text\n  bounds: [0,0,200,60]\n  content: {text: 中文, fontFamily: Noto Sans}\n')
        self.workspace=server.Workspace(source/'main.pptd',self.root/'working')
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.make_handler(self.workspace))
        self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.http.server_port)
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.tmp.cleanup()
    def test_missing_font_glyph_is_visible_to_browser_and_pdf(self):
        with urlopen(self.url+'/api/fonts') as response:result=json.load(response)
        self.assertEqual(result['fonts'],[])
        self.assertIn('missing glyph',result['errors'][0]['error'])
    def test_export_requires_object_instead_of_dropping_connection(self):
        for route in ('/api/export','/api/export-pdf','/api/edit'):
            with self.assertRaises(HTTPError) as cm:
                urlopen(Request(self.url+route,data=b'[]',headers={'Content-Type':'application/json'}))
            self.assertEqual(cm.exception.code,400)
            self.assertIn('object',json.load(cm.exception)['error'])
    def test_identity_prevents_port_collision_false_positive(self):
        with urlopen(self.url+'/api/identity') as response:result=json.load(response)
        self.assertEqual(result['workspace'],str(self.workspace.root))

if __name__=='__main__':unittest.main()
