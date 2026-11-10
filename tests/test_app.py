import os
import io
import re
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

    def test_streaming_download_errors_return_gateway_status_and_clean_temp_files(self):
        for failure, expected_status in (("timeout", 504), ("truncated", 502)):
            with self.subTest(failure=failure):
                release_body = threading.Event()

                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/pdf")
                        self.send_header("Content-Length", "100")
                        self.end_headers()
                        self.wfile.write(b"%PDF")
                        self.wfile.flush()
                        if failure == "timeout":
                            release_body.wait(timeout=2)
                        self.close_connection = True

                    def log_message(self, *_args):
                        pass

                server = HTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        with (
                            patch.object(main, "validate_remote_url"),
                            patch.object(main, "_validate_connected_peer"),
                            patch.object(main, "DOWNLOAD_TIMEOUT", 0.1),
                            patch.object(main.tempfile, "tempdir", directory),
                            TestClient(main.app) as client,
                        ):
                            response = client.post(
                                "/api/v1/parse",
                                json={"url": f"http://127.0.0.1:{server.server_port}/sample.pdf"},
                            )
                        self.assertEqual(response.status_code, expected_status, response.text)
                        self.assertEqual(
                            response.json()["detail"],
                            "document download timed out" if failure == "timeout" else "document download failed",
                        )
                        self.assertEqual(list(Path(directory).glob("unstructured-*")), [])
                finally:
                    release_body.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

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

    def test_repeated_image_resource_is_extracted_and_uploaded_once(self):
        path = self._make_pdf(two_images=True)
        extraction_calls = []
        original_extract = fitz.Document.extract_image

        def extract(document, xref):
            extraction_calls.append(xref)
            return original_extract(document, xref)

        class Storage:
            def __init__(self):
                self.keys = []

            def put(self, key, data, *, content_type):
                self.keys.append(key)
                return key

        try:
            with tempfile.TemporaryDirectory() as image_dir:
                with patch.object(fitz.Document, "extract_image", new=extract):
                    extracted = main._extract_pdf_images(path, image_dir, max_images=10, max_image_bytes=100000)
                self.assertEqual(len(extraction_calls), 1)
                storage = Storage()
                images = main._filter_image_instances(extracted["images"])
                uploaded, errors = main._upload_pdf_images(images, storage)
                self.assertEqual(errors, [])
                self.assertEqual(len(storage.keys), 1)
                self.assertEqual(len(uploaded), 2)
                self.assertEqual(uploaded[0]["image_uri"], uploaded[1]["image_uri"])
                self.assertNotEqual(uploaded[0]["bbox"], uploaded[1]["bbox"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_image_coordinates_include_pdf_page_rotation(self):
        path = self._make_pdf()
        try:
            with fitz.open(path) as document:
                document[0].set_rotation(90)
                document.saveIncr()
            with tempfile.TemporaryDirectory() as image_dir:
                extracted = main._extract_pdf_images(path, image_dir, max_images=10, max_image_bytes=100000)
            self.assertEqual(extracted["status"], "success")
            self.assertEqual(extracted["images"][0]["bbox"], [250.0, 20.0, 300.0, 120.0])
            self.assertEqual(extracted["images"][0]["page_width"], 400.0)
            self.assertEqual(extracted["images"][0]["page_height"], 300.0)
        finally:
            Path(path).unlink(missing_ok=True)

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
        self.assertEqual(result[1]["ocr_status"], "success")
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

    def test_pdf_text_only_parse_does_not_call_ocr_or_generate_markdown(self):
        path = self._make_pdf(two_images=False)
        try:
            calls = []

            def fake_partition(**kwargs):
                calls.append(kwargs)
                return []

            with (
                patch.object(main, "partition", side_effect=fake_partition),
                patch.object(main, "_markdown_for_pdf", side_effect=AssertionError("Markdown generation was called")),
            ):
                result = main._parse_document(path, "sample.pdf", ".pdf", extract_images=False)
            self.assertEqual([call["strategy"] for call in calls], ["fast"])
            self.assertEqual(result["text"], "")
            self.assertNotIn("markdown", result)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_image_only_pdf_keeps_images_without_native_text_or_ocr(self):
        path = self._make_pdf(two_images=True, include_text=False)
        try:
            with patch.object(main, "partition", return_value=[]):
                result = main._parse_document(path, "images.pdf", ".pdf")
            self.assertEqual(set(result), {"markdown"})
            self.assertEqual(result["markdown"].count("![PDF"), 2)
            self.assertIn("![PDF", result["markdown"])
            self.assertNotIn("text", result)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_pdf_text_source_is_unstructured_fast_in_both_modes(self):
        from unstructured.documents.elements import ElementMetadata, NarrativeText

        path = self._make_pdf()
        element = NarrativeText("Text from Unstructured only", metadata=ElementMetadata(page_number=1))
        try:
            with tempfile.TemporaryDirectory() as directory:
                with (
                    patch.object(main, "partition", return_value=[element]) as parser,
                    patch.object(fitz.Page, "get_text", side_effect=AssertionError("PyMuPDF text extraction")),
                    patch.object(main, "_extract_pdf_images", wraps=main._extract_pdf_images) as extract,
                    patch.object(main, "_build_storage_backend", return_value=main.LocalStorageBackend(directory)),
                ):
                    plain = main._parse_document(path, "sample.pdf", ".pdf", extract_images=False)
                    rich = main._parse_document(path, "sample.pdf", ".pdf", extract_images=True)
                self.assertEqual(parser.call_count, 2)
                self.assertEqual([call.kwargs["strategy"] for call in parser.call_args_list], ["fast", "fast"])
                self.assertEqual(extract.call_count, 1)
                self.assertEqual(plain, {"text": element.text})
                self.assertEqual(set(rich), {"markdown"})
                self.assertIn(element.text, rich["markdown"])
                self.assertNotIn("Hello native text", rich["markdown"])
                self.assertIn("![PDF", rich["markdown"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_markdown_inserts_image_between_unstructured_paragraphs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paragraphs.pdf"
            data = io.BytesIO()
            Image.new("RGB", (80, 80), "blue").save(data, format="PNG")
            with fitz.open() as document:
                page = document.new_page(width=300, height=400)
                page.insert_text((30, 40), "TOP paragraph before the image.")
                page.insert_image(fitz.Rect(30, 100, 130, 180), stream=data.getvalue())
                page.insert_text((30, 280), "BOTTOM paragraph after the image.")
                document.save(path)
            with patch.object(main, "_build_storage_backend", return_value=main.LocalStorageBackend(str(Path(directory) / "storage"))):
                plain = main._parse_document(str(path), path.name, ".pdf", extract_images=False)
                rich = main._parse_document(str(path), path.name, ".pdf", extract_images=True)
            markdown = rich["markdown"]
            self.assertLess(markdown.index("TOP paragraph"), markdown.index("![PDF"))
            self.assertLess(markdown.index("![PDF"), markdown.index("BOTTOM paragraph"))
            without_images = "\n\n".join(paragraph for paragraph in markdown.split("\n\n") if not paragraph.startswith("![PDF"))
            self.assertEqual(without_images, plain["text"])

    def test_pdf_markdown_text_failure_returns_error_and_cleans_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((40, 40), "Native text")
                document.save(path)
            with (
                patch.object(main, "_download_document", return_value=(str(path), path.name, ".pdf")),
                patch.object(main.tempfile, "tempdir", directory),
                patch.object(main, "partition", side_effect=RuntimeError("text extraction failed")),
                TestClient(main.app) as client,
            ):
                response = client.post("/api/v1/parse", json={"url": "https://example.com/sample.pdf"})
            self.assertEqual(response.status_code, 502, response.text)
            self.assertEqual(response.json()["detail"], "document text parsing failed; the file may be damaged or missing a format dependency")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_image_mode_keeps_markdown_when_no_images_are_returned(self):
        path = self._make_pdf()
        try:
            result = main._parse_document(path, "sample.pdf", ".pdf", max_images=0)
            self.assertEqual(result["warnings"], ["image limit reached"])
            self.assertIn("Hello native text", result["markdown"])
            self.assertNotIn("text", result)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_markdown_embeds_ocr_text_and_object_keys_without_metadata(self):
        path = self._make_pdf(two_images=True)

        class Adapter(main.VisionAdapter):
            def cache_identity(self):
                return "test-markdown-ocr"

            async def recognize_image(self, image_bytes, *, mime_type, metadata=None):
                return {"status": "success", "text": "First OCR line\nSecond OCR line"}

        try:
            with tempfile.TemporaryDirectory() as directory:
                with (
                    patch.object(main, "_build_storage_backend", return_value=main.LocalStorageBackend(directory)),
                    patch.object(main, "_build_vision_adapter", return_value=Adapter()),
                ):
                    result = main._parse_document(path, "sample.pdf", ".pdf", recognize_images=True, filter_images=False)
                self.assertEqual(set(result), {"markdown"})
                self.assertIn("Hello native text", result["markdown"])
                self.assertEqual(result["markdown"].count("![PDF"), 2)
                self.assertEqual(result["markdown"].count("> 图片识别文本：First OCR line\n> Second OCR line"), 2)
                links = re.findall(r"!\[[^\]]+\]\(([^)]+)\)", result["markdown"])
                for object_key in links:
                    self.assertTrue((Path(directory) / object_key).is_file())
                    self.assertEqual(Path(object_key).suffix, "")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_image_upload_failure_warns_without_returning_broken_references(self):
        path = self._make_pdf()

        class Storage:
            def put(self, *args, **kwargs):
                raise OSError("upload failed")

        try:
            with patch.object(main, "_build_storage_backend", return_value=Storage()):
                result = main._parse_document(path, "sample.pdf", ".pdf")
            self.assertEqual(set(result), {"markdown", "warnings"})
            self.assertIn("Hello native text", result["markdown"])
            self.assertNotIn("![PDF", result["markdown"])
            self.assertEqual(result["warnings"], ["page-1-image-1: upload failed: OSError"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_image_limit_on_later_page_returns_warning(self):
        path = self._make_pdf()
        try:
            with fitz.open(path) as document:
                xref = document[0].get_images()[0][0]
                page = document.new_page(width=300, height=400)
                page.insert_image(fitz.Rect(20, 100, 120, 150), xref=xref)
                document.saveIncr()
            result = main._parse_document(path, "sample.pdf", ".pdf", max_images=1)
            self.assertEqual(result["markdown"].count("![PDF"), 1)
            self.assertEqual(result["warnings"], ["image limit reached"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_pdf_page_limit_applies_to_text_and_markdown_images(self):
        markers = {1: "ALPHA content", 2: "BETA content", 100: "HUNDRED content", 101: "LAST content"}
        image = io.BytesIO()
        Image.new("RGB", (80, 80), "red").save(image, format="PNG")
        cases = [
            ({}, 100, {1, 2, 100}, True),
            ({"max_pages": 2}, 2, {1, 2}, True),
            ({"max_pages": 2, "extract_images": False}, 2, {1, 2}, False),
            ({"max_pages": 200}, 101, {1, 2, 100, 101}, True),
        ]
        for options, parsed_pages, included_pages, extract_images in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "sample.pdf")
                with fitz.open() as document:
                    for page_number in range(1, 102):
                        page = document.new_page(width=300, height=400)
                        if page_number in markers:
                            page.insert_text((40, 40), markers[page_number])
                            page.insert_image(fitz.Rect(20, 100, 120, 200), stream=image.getvalue())
                    document.save(path)
                with (
                    patch.object(main, "_download_document", return_value=(path, "sample.pdf", ".pdf")),
                    patch.object(main, "_build_storage_backend", return_value=main.LocalStorageBackend(str(Path(directory) / "storage"))),
                    patch.object(main.tempfile, "tempdir", directory),
                    TestClient(main.app) as client,
                ):
                    response = client.post(
                        "/api/v1/parse",
                        json={"url": "https://example.com/sample.pdf", **options},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                content_field, absent_field = ("markdown", "text") if extract_images else ("text", "markdown")
                self.assertIn(content_field, result)
                self.assertNotIn(absent_field, result)
                for page_number, marker in markers.items():
                    if page_number in included_pages:
                        self.assertIn(marker, result[content_field])
                    else:
                        self.assertNotIn(marker, result[content_field])
                expected_fields = {content_field}
                if parsed_pages < 101:
                    expected_fields.add("warnings")
                    self.assertEqual(result["warnings"], [f"PDF contains 101 pages; only the first {parsed_pages} pages were parsed"])
                self.assertEqual(set(result), expected_fields)
                if extract_images:
                    image_links = re.findall(r"!\[PDF 第 (\d+) 页图片\]\(([^)]+)\)", result["markdown"])
                    self.assertEqual({int(page) for page, _ in image_links}, included_pages)
                    self.assertEqual(len(image_links), len(included_pages))
                    for _, object_key in image_links:
                        self.assertTrue((Path(directory) / "storage" / object_key).is_file())
                        self.assertFalse(object_key.startswith(("http:", "https:")))
                        self.assertEqual(Path(object_key).suffix, "")
                self.assertFalse(Path(path).exists())
                self.assertEqual(list(Path(directory).glob("unstructured-*")), [])

    def test_pdf_page_limit_rejects_nonpositive_values_before_download(self):
        with patch.object(main, "_download_document") as download, TestClient(main.app) as client:
            for max_pages in (0, -1):
                with self.subTest(max_pages=max_pages):
                    response = client.post(
                        "/api/v1/parse",
                        json={"url": "https://example.com/sample.pdf", "max_pages": max_pages},
                    )
                    self.assertEqual(response.status_code, 422)
            download.assert_not_called()

    def test_pdf_page_preparation_failure_returns_download_error_and_cleans_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.pdf"
            path.write_bytes(b"%PDF-broken")
            with (
                patch.object(main, "_download_document", return_value=(str(path), "broken.pdf", ".pdf")),
                patch.object(main.tempfile, "tempdir", directory),
                TestClient(main.app) as client,
            ):
                response = client.post("/api/v1/parse", json={"url": "https://example.com/broken.pdf"})
            self.assertEqual(response.status_code, 502, response.text)
            self.assertEqual(list(Path(directory).iterdir()), [])

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
        for extract_images in (True, False):
            with self.subTest(extract_images=extract_images):
                fd, path = tempfile.mkstemp(suffix=".txt")
                os.write(fd, b"hello from a test")
                os.close(fd)
                try:
                    with (
                        TestClient(main.app) as client,
                        patch.object(main, "_download_document", return_value=(path, "sample.txt", ".txt")),
                    ):
                        response = client.post(
                            "/api/v1/parse",
                            json={"url": "https://example.com/sample.txt", "extract_images": extract_images},
                        )
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    content_field, absent_field = ("markdown", "text") if extract_images else ("text", "markdown")
                    self.assertEqual(result[content_field], "hello from a test")
                    self.assertNotIn(absent_field, result)
                    self.assertEqual(set(result), {content_field})
                    self.assertFalse(Path(path).exists())
                finally:
                    Path(path).unlink(missing_ok=True)

    def test_native_document_formats_parse_with_the_compatible_dependency_versions(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation

        marker = "Compatibility format verification"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = Document()
            document.add_paragraph(marker)
            document.save(root / "sample.docx")
            presentation = Presentation()
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = marker
            presentation.save(root / "sample.pptx")
            workbook = Workbook()
            workbook.active.append(["title"])
            workbook.active.append([marker])
            workbook.save(root / "sample.xlsx")
            for extension, content in (
                ("txt", marker),
                ("csv", "id,title\n1," + marker),
                ("md", "# " + marker),
                ("html", "<html><body><p>" + marker + "</p></body></html>"),
            ):
                (root / ("sample." + extension)).write_text(content)
            for path in sorted(root.iterdir()):
                with self.subTest(format=path.suffix):
                    result = main._parse_document(str(path), path.name, path.suffix, extract_images=False)
                    self.assertEqual(set(result), {"text"})
                    self.assertIn(marker, result["text"])


if __name__ == "__main__":
    unittest.main()
