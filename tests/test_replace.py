import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('synodocfic_server', ROOT / 'package/app/server.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

class ReplacementTests(unittest.TestCase):
    def test_exact_replacement_and_spaces(self):
        for old, find, replacement, expected in [
            ('Mon_fichier_test.txt', '_', ' ', 'Mon fichier test.txt'),
            ('a--b.txt', '--', '  ', 'a  b.txt'),
            ('a_b.txt', '_', ' x ', 'a x b.txt'),
            ('a  b.txt', '  ', ' ', 'a b.txt'),
            ('a_b.txt', '_', '', 'ab.txt'),
            ('Aa.txt', 'a', 'x', 'Ax.txt'),
            ('a.txt', '.txt', '.md', 'a.txt'),
            ('Mon.fichier.test.pdf', '.', ' ', 'Mon fichier test.pdf'),
            ('Mon.fichier.test.PDF', '.', ' ', 'Mon fichier test.PDF'),
            ('a.txt', 'txt', 'pdf', 'a.txt'),
            ('sans_extension', '_', ' ', 'sans extension'),
            ('.hidden', 'hidden', 'secret', '.secret'),
            ('archive.tar.gz', '.', ' ', 'archive tar.gz'),
            ('a.txt', 'missing', ' ', 'a.txt'),
        ]:
            with self.subTest(old=old, find=find, replacement=replacement):
                self.assertEqual(backend.transform_filename(old, 'replace', find, replacement), expected)
        with self.assertRaises(ValueError):
            backend.transform_filename('a.txt', 'replace', '', ' ')
        self.assertEqual(backend.transform_filename('a b.txt', 'remove', ' '), 'ab.txt')
        self.assertEqual(backend.transform_filename('a.txt', 'prefix', 'x'), 'xa.txt')
        self.assertEqual(backend.transform_filename('a.txt', 'suffix', 'x'), 'ax.txt')

    def test_http_preview_rename_history_and_undo(self):
        with tempfile.TemporaryDirectory(prefix='synodocfic-rename-') as temp:
            root = Path(temp)
            files = root / 'files'
            files.mkdir()
            (files / 'a.b.txt').write_text('original', encoding='utf-8')
            (files / 'a b.txt').write_text('existing', encoding='utf-8')
            server = backend.ThreadingHTTPServer(('127.0.0.1', 0), backend.RequestHandler)
            server.state = backend.AppState(root / 'state')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def post(path, payload):
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    connection.request('POST', path, json.dumps(payload), {'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            try:
                # Only bypass the NAS-specific /volume path check for the local fixture.
                with patch.object(backend, 'is_valid_target_directory', return_value=True):
                    status, error = post('/api/preview', {'directory': str(files), 'mode': 'replace', 'value': '', 'replacement': ' '})
                    self.assertEqual(status, 400)
                    status, preview = post('/api/preview', {'directory': str(files), 'mode': 'replace', 'value': '.', 'replacement': ' '})
                self.assertEqual(status, 200)
                self.assertEqual(preview['summary']['rename_actions'], 1)
                self.assertEqual(preview['summary']['auto_renamed'], 1)
                self.assertTrue((files / 'a.b.txt').exists(), 'Preview must not rename files')
                status, result = post('/api/rename', {'preview_id': preview['preview_id'], 'confirm': True})
                self.assertEqual(status, 200, result)
                self.assertEqual((files / 'a b (1).txt').read_text(), 'original')
                self.assertEqual((files / 'a b.txt').read_text(), 'existing')
                history = server.state.get_history()
                self.assertEqual(history[0]['request']['replacement'], ' ')
                status, result = post('/api/undo', {})
                self.assertEqual(status, 200, result)
                self.assertEqual((files / 'a.b.txt').read_text(), 'original')
                self.assertFalse((files / 'a b (1).txt').exists())
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

if __name__ == '__main__':
    unittest.main()
