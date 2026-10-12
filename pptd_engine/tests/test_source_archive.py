import base64
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree
from pptx import Presentation

from pptd_engine.source_archive import NAMESPACE, embed_source, recover_source


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, files, entry='doc.pptd'):
        stream = io.BytesIO()
        with ZipFile(stream, 'w') as archive:
            for name, content in files:
                archive.writestr(name, content)
        data = stream.getvalue()
        root = etree.Element('{' + NAMESPACE + '}source', entry=entry, sha256=hashlib.sha256(data).hexdigest())
        root.text = base64.b64encode(data).decode()
        pptx = self.root / 'source.pptx'
        with ZipFile(pptx, 'w') as package:
            package.writestr('customXml/pptdSource.xml', etree.tostring(root))
        return pptx

    def test_late_invalid_member_does_not_leave_partial_recovery(self):
        pptx = self.snapshot([('doc.pptd', 'version: v2'), ('pages/1.page', 'elements: []'), ('../escape.txt', 'bad')])
        destination = self.root / 'recovered'
        with self.assertRaises(ValueError):
            recover_source(pptx, destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_invalid_entry_is_rejected_before_writing(self):
        for index, entry in enumerate(['../outside.pptd', '/tmp/outside.pptd', 'pages\\outside.pptd', 'absent.pptd', 'pages/1.page']):
            with self.subTest(entry=entry):
                pptx = self.snapshot([('doc.pptd', 'version: v2'), ('pages/1.page', 'elements: []')], entry)
                destination = self.root / f'recovered-{index}'
                with self.assertRaises(ValueError):
                    recover_source(pptx, destination)
                self.assertFalse(destination.exists())

    def test_preexisting_empty_destination_stays_empty_after_failure(self):
        pptx = self.snapshot([('doc.pptd', 'version: v2'), ('../bad', 'bad')])
        destination = self.root / 'recovered'; destination.mkdir()
        with self.assertRaises(ValueError):
            recover_source(pptx, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_full_source_roundtrip_preserves_bytes(self):
        source = self.root / 'project'; (source / 'pages').mkdir(parents=True); (source / 'media').mkdir()
        files = {
            'doc.pptd': b'version: v2\nsize: [960, 540]\npages: [pages/1.page]\n',
            'pages/1.page': b'# authored comment\nelements: [{elementId: image, elementType: image, src: media/x.png, bounds: [1, 2, 3, 4]}]\n',
            'media/x.png': b'\x89PNG\x00binary fixture',
        }
        for name, content in files.items(): (source / name).write_bytes(content)
        pptx = self.root / 'presentation.pptx'; Presentation().save(pptx)
        embed_source(pptx, source / 'doc.pptd')
        destination = self.root / 'recovered'; destination.mkdir()
        entry = recover_source(pptx, destination)
        self.assertEqual(entry, destination / 'doc.pptd')
        self.assertEqual({str(p.relative_to(destination)): p.read_bytes() for p in destination.rglob('*') if p.is_file()}, files)

    def test_nonempty_destination_is_never_overwritten(self):
        pptx = self.snapshot([('doc.pptd', 'version: v2')])
        destination = self.root / 'recovered'; destination.mkdir(); (destination / 'owned.txt').write_text('keep')
        with self.assertRaises(ValueError): recover_source(pptx, destination)
        self.assertEqual((destination / 'owned.txt').read_text(), 'keep')


if __name__ == '__main__': unittest.main()
