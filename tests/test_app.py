import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main


class _Handler(BaseHTTPRequestHandler):
    payload = b"0123456789"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args):
        pass


class AppTests(unittest.TestCase):
    def test_rejects_ssrf_targets_and_non_http(self):
        with self.assertRaises(main.URLRejected):
            main.validate_remote_url("http://127.0.0.1/example.pdf")
        with self.assertRaises(main.URLRejected):
            main.validate_remote_url("file:///etc/passwd")

    def test_download_enforces_stream_limit(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/sample.pdf"
        try:
            with patch.object(main, "validate_remote_url"), patch.object(main, "MAX_FILE_SIZE", 4):
                with self.assertRaises(main.DownloadTooLarge):
                    main._download_document(url)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_corrupt_pdf_signature_is_rejected(self):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"this is not a pdf")
        os.close(fd)
        try:
            with self.assertRaises(main.DownloadError):
                main._check_signature(path, ".pdf")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_endpoint_cleans_temp_file(self):
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.write(fd, b"hello from a test")
        os.close(fd)
        client = TestClient(main.app)
        try:
            with patch.object(main, "_download_document", return_value=(path, "sample.txt", ".txt")):
                response = client.post("/api/v1/parse", json={"url": "https://example.com/sample.txt"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["text"], "hello from a test")
            self.assertFalse(Path(path).exists())
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
