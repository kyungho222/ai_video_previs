import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from fastapi.testclient import TestClient


class ConsoleBundleTests(unittest.TestCase):
    def test_console_contains_application_and_backend_origin_helper_imports(self):
        from service_app import create_app
        with TemporaryDirectory() as folder:
            client=TestClient(create_app(previs_root=Path(folder),python_client=SimpleNamespace(),agent=SimpleNamespace()))
            response=client.get('/previs-console01.html')
            self.assertEqual(response.status_code,200)
            self.assertIn("import(apiBase + '/previs-console.js')",response.text)
            self.assertIn("import(apiBase + '/usage-history.js')",response.text)
            self.assertIn("import(apiBase + '/api-client.js')",response.text)
            self.assertNotIn('/app.js',response.text)
            self.assertEqual(client.get('/app.js').status_code,404)
            self.assertEqual(client.get('/usage-history.js').status_code,200)

    def test_console_serves_separate_feature_modules(self):
        from service_app import create_app
        with TemporaryDirectory() as folder:
            client = TestClient(create_app(previs_root=Path(folder), python_client=SimpleNamespace(), agent=SimpleNamespace()))
            console = client.get('/previs-console01.html')
            self.assertIn('aria-controls="shortform-panel"', console.text)
            self.assertIn('aria-labelledby="console-shortform-tab"', console.text)
            self.assertIn('<style id="shortform-styles">', console.text)
            self.assertIn("import(apiBase + '/shortform.js')", console.text)
            for asset, initializer in (
                ('shortform.js', 'initShortform'),
                ('previs-console.js', 'initPrevis'),
                ('usage-history.js', 'initUsageHistory'),
            ):
                module = client.get('/' + asset)
                self.assertEqual(module.status_code, 200)
                self.assertIn(f'export function {initializer}(', module.text)
                self.assertNotIn(f'function {initializer}(', console.text)

    def test_shortform_displays_payload_above_response(self):
        from service_app import create_app
        with TemporaryDirectory() as folder:
            client = TestClient(create_app(previs_root=Path(folder), python_client=SimpleNamespace(), agent=SimpleNamespace()))
            console = client.get('/previs-console01.html').text
            payload = console.index('<pre id="sf-payload"')
            response = console.index('<pre id="sf-response"')
            self.assertLess(payload, response)
