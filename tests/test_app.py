import os
import io
import hashlib
import time

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image

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

    def test_pdf_images_keep_duplicate_instances_and_coordinates(self):
        path = self._make_pdf(two_images=True)
        image_dir = tempfile.mkdtemp()
        try:
            result = main._extract_pdf_images(path, image_dir, max_images=10, max_image_bytes=100000)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["count"], 2)
            self.assertEqual([item["page_number"] for item in result["images"]], [1, 1])
            self.assertEqual(result["images"][0]["image_id"], "page-1-image-1")
            self.assertNotEqual(result["images"][0]["bbox"], result["images"][1]["bbox"])
            self.assertTrue(Path(image_dir, "page-1-image-1.png").exists())
        finally:
            Path(path).unlink(missing_ok=True)
            import shutil
            shutil.rmtree(image_dir, ignore_errors=True)

    def test_storage_backend_uses_extensionless_object_key(self):
        path = self._make_pdf(two_images=False)
        image_dir = tempfile.mkdtemp()

        class Storage:
            def __init__(self):
                self.calls = []

            def put(self, key, data, *, content_type):
                self.calls.append((key, content_type, data))
                return key

        storage = Storage()
        try:
            result = main._extract_pdf_images(
                path, image_dir, max_images=10, max_image_bytes=100000
            )
            result["images"], upload_errors = main._upload_pdf_images(result["images"], storage, max_workers=2)
            self.assertEqual(result["count"], 1)
            self.assertEqual(upload_errors, [])
            self.assertEqual(len(storage.calls), 1)
            self.assertNotIn(".", storage.calls[0][0].rsplit("/", 1)[-1])
            self.assertEqual(result["images"][0]["image_uri"], storage.calls[0][0])
        finally:
            Path(path).unlink(missing_ok=True)
            import shutil
            shutil.rmtree(image_dir, ignore_errors=True)

    def test_storage_uploads_run_with_bounded_concurrency(self):
        image_dir = tempfile.mkdtemp()
        images = []
        for index in range(4):
            path = Path(image_dir, f"image-{index}.png")
            path.write_bytes(b"image")
            images.append({"_path": str(path), "_object_key": f"objects/{index}", "format": "png"})

        class Storage:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def put(self, key, data, *, content_type):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.03)
                with self.lock:
                    self.active -= 1
                return key

        storage = Storage()
        try:
            result, errors = main._upload_pdf_images(images, storage, max_workers=2)
            self.assertEqual(errors, [])
            self.assertEqual([image["image_uri"] for image in result], [f"objects/{i}" for i in range(4)])
            self.assertGreaterEqual(storage.max_active, 2)
            self.assertLessEqual(storage.max_active, 2)
        finally:
            import shutil
            shutil.rmtree(image_dir, ignore_errors=True)

    def test_image_filter_hashes_and_skips_small_images(self):
        images = [
            self._image_instance("small", b"small", width=20, height=20, bbox=(0, 0, 20, 20)),
            self._image_instance("large", b"large", width=200, height=200, bbox=(30, 30, 230, 230)),
        ]
        result = main._filter_image_instances(images)
        self.assertEqual(result[0]["resource_hash"], hashlib.sha256(b"small").hexdigest())
        self.assertEqual(result[0]["filter_status"], "SKIP")
        self.assertIn("width_below_minimum", result[0]["filter_reasons"])
        self.assertEqual(result[1]["filter_status"], "CANDIDATE")

    def test_exact_duplicate_keeps_instances_and_marks_reuse(self):
        images = [
            self._image_instance("first", b"same", page=1, bbox=(20, 20, 120, 120)),
            self._image_instance("second", b"same", page=2, bbox=(20, 20, 120, 120)),
        ]
        result = main._filter_image_instances(images)
        self.assertEqual(result[0]["filter_status"], "CANDIDATE")
        self.assertEqual(result[1]["filter_status"], "REUSE")
        self.assertEqual(result[1]["duplicate_of"], "first")

    def test_repeated_header_resource_is_skipped_after_three_pages(self):
        images = [
            self._image_instance(f"logo-{page}", b"logo", page=page, bbox=(10, 10, 60, 40))
            for page in (1, 2, 3)
        ]
        result = main._filter_image_instances(images)
        self.assertTrue(all(item["filter_status"] == "SKIP" for item in result))
        self.assertTrue(all("repeated_header_or_footer" in item["filter_reasons"] for item in result))

    def test_recognition_only_calls_unique_candidates_and_reuses_duplicate(self):
        images = [
            self._image_instance("first", b"same", page=1, bbox=(20, 20, 120, 120)),
            self._image_instance("second", b"same", page=2, bbox=(20, 20, 120, 120)),
            self._image_instance("other", b"other", page=3, bbox=(20, 20, 120, 120)),
        ]
        images = main._filter_image_instances(images)

        class Adapter(main.VisionAdapter):
            def __init__(self):
                self.calls = []

            def cache_identity(self):
                return "test-provider:test-model:v1"

            async def recognize_image(self, image_bytes, *, mime_type, metadata=None):
                self.calls.append(metadata["image_id"])
                return {"status": "success", "text": f"text-{metadata['image_id']}"}

        adapter = Adapter()
        result, stats = main._recognize_images_with_stats(images, adapter, recognize_images=True, max_recognition_images=20)
        self.assertEqual(adapter.calls, ["first", "other"])
        self.assertEqual(result[1]["recognition_status"], "REUSE")
        self.assertEqual(result[1]["ocr_text"], result[0]["ocr_text"])
        self.assertEqual(stats["estimated_api_calls"], 2)

    def test_recognition_cache_avoids_second_call(self):
        image = self._image_instance("cached", b"cache-me", bbox=(20, 20, 120, 120))

        class Adapter(main.VisionAdapter):
            calls = 0

            def cache_identity(self):
                return "cache-test:model:v1"

            async def recognize_image(self, image_bytes, *, mime_type, metadata=None):
                self.calls += 1
                return {"status": "success", "text": "cached result"}

        adapter = Adapter()
        main._recognize_images_with_stats(main._filter_image_instances([image]), adapter, recognize_images=True, max_recognition_images=20)
        second_image = self._image_instance("cached-again", b"cache-me", bbox=(20, 20, 120, 120))
        result, stats = main._recognize_images_with_stats(main._filter_image_instances([second_image]), adapter, recognize_images=True, max_recognition_images=20)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(result[0]["recognition_status"], "REUSED")
        self.assertEqual(stats["estimated_api_calls"], 0)

    def test_recognition_budget_stops_external_calls(self):
        images = main._filter_image_instances([
            self._image_instance("one", b"one", bbox=(20, 20, 120, 120)),
            self._image_instance("two", b"two", bbox=(20, 20, 120, 120)),
        ])

        class Adapter(main.VisionAdapter):
            def __init__(self):
                self.calls = 0

            def cache_identity(self):
                return "budget-test:v1"

            async def recognize_image(self, image_bytes, *, mime_type, metadata=None):
                self.calls += 1
                return {"status": "success", "text": "ok"}

        adapter = Adapter()
        result, stats = main._recognize_images_with_stats(images, adapter, recognize_images=True, max_recognition_images=1)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(stats["estimated_api_calls"], 1)
        self.assertEqual(result[1]["recognition_status"], "BUDGET_EXCEEDED")

    def _image_instance(self, image_id, data, *, page=1, width=200, height=200, bbox=(20, 20, 220, 220)):
        return {
            "image_id": image_id,
            "page_number": page,
            "bbox": list(bbox),
            "width": width,
            "height": height,
            "format": "png",
            "_image_bytes": data,
            "_object_key": f"objects/{image_id}",
            "_path": "",
        }

    def test_pdf_parse_does_not_call_ocr_and_returns_markdown(self):
        path = self._make_pdf(two_images=False)
        try:
            calls = []

            def fake_partition(**kwargs):
                calls.append(kwargs)
                return []

            with patch.object(main, "partition", side_effect=fake_partition):
                result = main._parse_document(path, "sample.pdf", ".pdf", True, True, extract_images=False)
            self.assertEqual(result["strategy"], "fast")
            self.assertEqual(result["text_parse_status"], "success_empty")
            self.assertEqual([call["strategy"] for call in calls], ["fast"])
            self.assertIn("markdown", result)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_image_only_pdf_still_extracts_images_when_fast_text_is_empty(self):
        path = self._make_pdf(two_images=True, include_text=False)
        try:
            with patch.object(main, "partition", return_value=[]):
                result = main._parse_document(path, "images.pdf", ".pdf", True, True)
            self.assertEqual(result["text_parse_status"], "success_empty")
            self.assertEqual(result["image_count"], 2)
            self.assertEqual(result["recognized_image_count"], 0)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_vision_disabled_does_not_call_adapter(self):
        image = {"image_id": "page-1-image-1", "image_bytes": b"x", "format": "png"}
        adapter = type("Adapter", (), {"recognize_image": lambda *_args, **_kwargs: self.fail("called")})()
        result = main._recognize_images([image], adapter, recognize_images=False)
        self.assertEqual(result[0]["ocr_status"], "disabled")

    def test_configured_vision_result_is_attached_to_image(self):
        image = {"image_id": "page-2-image-1", "image_bytes": b"x", "format": "png", "page_number": 2}

        class Adapter(main.VisionAdapter):
            async def recognize_image(self, image_bytes, *, mime_type, metadata=None):
                return {"status": "success", "text": "recognized text"}

        result = main._recognize_images([image], Adapter(), recognize_images=True)
        self.assertEqual(result[0]["ocr_status"], "success")
        self.assertEqual(result[0]["ocr_text"], "recognized text")

    def test_vision_failure_preserves_image_and_records_error(self):
        image = {"image_id": "page-1-image-1", "image_bytes": b"x", "format": "png", "page_number": 1}

        class Adapter(main.VisionAdapter):
            async def recognize_image(self, image_bytes, *, mime_type, metadata=None):
                raise TimeoutError("mock timeout")

        result = main._recognize_images([image], Adapter(), recognize_images=True)
        self.assertEqual(result[0]["image_id"], "page-1-image-1")
        self.assertEqual(result[0]["ocr_status"], "failed")
        self.assertEqual(result[0]["ocr_error"], "TimeoutError")

    def _make_pdf(self, two_images=False, include_text=True):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        image = Image.new("RGB", (20, 10), "red")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        document = fitz.open()
        page = document.new_page(width=300, height=400)
        if include_text:
            page.insert_text((40, 40), "Hello native text")
        page.insert_image(fitz.Rect(20, 100, 120, 150), stream=buffer.getvalue())
        if two_images:
            page.insert_image(fitz.Rect(150, 200, 250, 250), stream=buffer.getvalue())
        document.save(path)
        document.close()
        return path

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
