from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlsplit

# Keep the library's commercial offering notice out of service logs.
os.environ.setdefault("UNSTRUCTURED_DISABLE_NOTICE", "1")

import requests
import fitz
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from unstructured.partition.auto import partition

SERVICE_NAME = "unstructured-api"
DEFAULT_PORT = 4763
DEFAULT_DOWNLOAD_TIMEOUT = 20.0
DEFAULT_MAX_REDIRECTS = 3
SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv",
    ".txt", ".md", ".markdown", ".html", ".htm", ".jpg", ".jpeg", ".png",
    ".tiff", ".tif", ".bmp",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
PDF_EXTENSIONS = {".pdf"}
TEXT_EXTENSIONS = {".csv", ".txt", ".md", ".markdown", ".html", ".htm"}
MIME_TO_EXTENSION = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
}
DOCUMENT_MIME_TYPES = set(MIME_TO_EXTENSION) | {"application/octet-stream", "binary/octet-stream"}
logger = logging.getLogger(SERVICE_NAME)
# pdfminer warning messages include the temporary filename; keep service logs path-free.
logging.getLogger("pdfminer.pdfpage").setLevel(logging.ERROR)


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE_MB", 50) * 1024 * 1024
DOWNLOAD_TIMEOUT = _env_float("DOWNLOAD_TIMEOUT_SECONDS", DEFAULT_DOWNLOAD_TIMEOUT)
MAX_REDIRECTS = _env_int("MAX_REDIRECTS", DEFAULT_MAX_REDIRECTS)
MAX_PDF_IMAGES = _env_int("MAX_PDF_IMAGES", 100)
MAX_PDF_TOTAL_IMAGE_BYTES = _env_int("MAX_PDF_TOTAL_IMAGE_MB", 100) * 1024 * 1024
STORAGE_UPLOAD_CONCURRENCY = _env_int("STORAGE_UPLOAD_CONCURRENCY", 4)
MIN_IMAGE_WIDTH = _env_int("MIN_IMAGE_WIDTH", 64)
MIN_IMAGE_HEIGHT = _env_int("MIN_IMAGE_HEIGHT", 64)
MIN_IMAGE_AREA_RATIO = float(os.getenv("MIN_IMAGE_AREA_RATIO", "0.005"))
MIN_IMAGE_BYTES = _env_int("MIN_IMAGE_BYTES", 0)
IMAGE_SIZE_FILTER_ENABLED = _env_bool("IMAGE_SIZE_FILTER_ENABLED", True)
HEADER_REGION_RATIO = float(os.getenv("HEADER_REGION_RATIO", "0.12"))
FOOTER_REGION_RATIO = float(os.getenv("FOOTER_REGION_RATIO", "0.12"))
REPEATED_POSITION_TOLERANCE = float(os.getenv("REPEATED_POSITION_TOLERANCE", "0.02"))
MIN_REPEATED_PAGES = _env_int("MIN_REPEATED_PAGES", 3)
MAX_RECOGNITION_IMAGES = _env_int("MAX_RECOGNITION_IMAGES", 20)
RECOGNITION_CACHE_MAX_ENTRIES = _env_int("RECOGNITION_CACHE_MAX_ENTRIES", 1000)
RECOGNITION_CACHE_VERSION = os.getenv("RECOGNITION_CACHE_VERSION", "v1")
VISION_PROMPT_VERSION = os.getenv("VISION_PROMPT_VERSION", "v1")
STORAGE_DRIVER = os.getenv("STORAGE_DRIVER", "local").lower()
STORAGE_LOCAL_ROOT = os.getenv("STORAGE_LOCAL_ROOT", "./var/storage")
STORAGE_OSS_ENDPOINT = os.getenv("STORAGE_ALIYUN_OSS_ENDPOINT", "")
STORAGE_OSS_BUCKET = os.getenv("STORAGE_ALIYUN_OSS_BUCKET", "")
STORAGE_OSS_ACCESS_KEY_ID = os.getenv("STORAGE_ALIYUN_OSS_ACCESS_KEY_ID", "")
STORAGE_OSS_ACCESS_KEY_SECRET = os.getenv("STORAGE_ALIYUN_OSS_ACCESS_KEY_SECRET", "")
STORAGE_OSS_TIMEOUT = _env_float("STORAGE_ALIYUN_OSS_TIMEOUT_MS", 120000) / 1000
STORAGE_OSS_KEY_PREFIX = os.getenv("STORAGE_ALIYUN_OSS_KEY_PREFIX", "").strip("/")
VISION_TIMEOUT = _env_float("VISION_TIMEOUT_SECONDS", 30.0)
VISION_MAX_RETRIES = _env_int("VISION_MAX_RETRIES", 1)

_recognition_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
_recognition_cache_lock = threading.Lock()


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, description="HTTP or HTTPS URL of a document")
    extract_images: bool = Field(
        default=True,
        description="Extract embedded PDF images and return markdown; when false, return text instead of markdown.",
    )
    max_pages: int = Field(
        default=100,
        ge=1,
        description="Parse the first N PDF pages for text, images and Markdown; skip later pages without an error.",
    )
    recognize_images: bool = False
    max_images: int = Field(default=100, ge=0, le=1000)
    max_image_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
        description="Maximum bytes per extracted image; does not control the source document size.",
    )
    filter_images: bool = True
    max_recognition_images: int = Field(default=MAX_RECOGNITION_IMAGES, ge=0, le=1000)

    @field_validator("url")
    @classmethod
    def validate_url_shape(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        return value


class MarkdownResponse(BaseModel):
    markdown: str = Field(description="Document Markdown with image object keys and available OCR text.")
    warnings: Optional[List[str]] = Field(default=None, description="Present only when content was truncated or processing was incomplete.")


class TextResponse(BaseModel):
    text: str = Field(description="Document plain text when image extraction is disabled.")
    warnings: Optional[List[str]] = Field(default=None, description="Present only when content was truncated or processing was incomplete.")


class StorageBackend(ABC):
    @abstractmethod
    def put(self, path: str, data: bytes, *, content_type: str) -> str:
        raise NotImplementedError


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root)

    def put(self, path: str, data: bytes, *, content_type: str) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return path


class AliyunOSSStorageBackend(StorageBackend):
    def __init__(self, endpoint: str, bucket: str, access_key_id: str, access_key_secret: str):
        import oss2
        self.bucket = oss2.Bucket(
            oss2.Auth(access_key_id, access_key_secret),
            endpoint,
            bucket,
            connect_timeout=STORAGE_OSS_TIMEOUT,
        )

    def put(self, path: str, data: bytes, *, content_type: str) -> str:
        import oss2
        headers = {"Content-Type": content_type}
        result = self.bucket.put_object(path, data, headers=headers)
        if result.status >= 300:
            raise RuntimeError(f"OSS upload failed with HTTP {result.status}")
        return path


def _build_storage_backend() -> StorageBackend:
    if STORAGE_DRIVER == "aliyun_oss":
        required = [STORAGE_OSS_ENDPOINT, STORAGE_OSS_BUCKET, STORAGE_OSS_ACCESS_KEY_ID, STORAGE_OSS_ACCESS_KEY_SECRET]
        if not all(required):
            raise RuntimeError("Aliyun OSS storage is not fully configured")
        return AliyunOSSStorageBackend(STORAGE_OSS_ENDPOINT, STORAGE_OSS_BUCKET, STORAGE_OSS_ACCESS_KEY_ID, STORAGE_OSS_ACCESS_KEY_SECRET)
    return LocalStorageBackend(STORAGE_LOCAL_ROOT)

class DownloadError(Exception):
    status_code = 502


class URLRejected(DownloadError):
    status_code = 400


class UnsupportedDocument(DownloadError):
    status_code = 415


class DownloadTooLarge(DownloadError):
    status_code = 413


class DownloadTimedOut(DownloadError):
    status_code = 504


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
    )


def validate_remote_url(url: str) -> None:
    """Validate scheme, credentials and every resolved address before connecting."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise URLRejected("invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLRejected("only http and https URLs are allowed")
    if not parsed.hostname:
        raise URLRejected("URL host is required")
    if parsed.username is not None or parsed.password is not None:
        raise URLRejected("URL credentials are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLRejected("invalid URL port") from exc
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLRejected("URL host cannot be resolved") from exc
    if not addresses:
        raise URLRejected("URL host has no usable address")
    for address in {item[4][0] for item in addresses}:
        try:
            public = _is_public_ip(address)
        except ValueError as exc:
            raise URLRejected("URL host resolved to an invalid address") from exc
        if not public:
            raise URLRejected("URL resolves to a private or reserved network address")


def _safe_basename(name: str) -> str:
    name = unquote(name).replace("\\", "/")
    return Path(name).name or "document"


def _filename_from_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> Optional[str]:
    value = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^;\"]+)", value, flags=re.IGNORECASE)
    return _safe_basename(match.group(1).strip()) if match else None


def _extension_for_response(url: str, headers: requests.structures.CaseInsensitiveDict[str]) -> str:
    path_name = _safe_basename(urlsplit(url).path)
    path_ext = Path(path_name).suffix.lower()
    header_name = _filename_from_headers(headers)
    header_ext = Path(header_name).suffix.lower() if header_name else ""
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    mime_ext = MIME_TO_EXTENSION.get(content_type, "")
    ext = path_ext if path_ext in SUPPORTED_EXTENSIONS else header_ext
    if ext not in SUPPORTED_EXTENSIONS:
        ext = mime_ext
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocument("document format is unsupported or has no recognized extension")
    if content_type and content_type not in DOCUMENT_MIME_TYPES and not content_type.endswith("+xml"):
        raise UnsupportedDocument("remote response is not a supported document content type")
    if content_type in MIME_TO_EXTENSION and ext != MIME_TO_EXTENSION[content_type]:
        if content_type.startswith("text/") and ext in TEXT_EXTENSIONS:
            return ext
        raise UnsupportedDocument("URL extension and response content type disagree")
    return ext


def _check_signature(path: str, extension: str) -> None:
    with open(path, "rb") as handle:
        head = handle.read(16)
    if not head:
        raise DownloadError("remote document is empty")
    if extension == ".pdf" and not head.startswith(b"%PDF"):
        raise DownloadError("response is not a valid PDF document")
    if extension in {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls"} and not (
        head.startswith(b"PK") or head.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    ):
        raise DownloadError("response is not a valid Office document")


def _validate_connected_peer(response: requests.Response) -> None:
    """Reject a connection that resolved to an internal address after preflight DNS checks."""
    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
    sock = getattr(connection, "sock", None)
    if sock is None:
        # DNS is still checked before every request; some urllib3 versions do not expose the socket.
        return
    try:
        peer = sock.getpeername()[0]
        if not _is_public_ip(peer):
            raise URLRejected("document connection resolved to a private or reserved address")
    except (OSError, ValueError) as exc:
        raise DownloadError("document connection could not be verified") from exc


def _download_document(url: str) -> Tuple[str, str, str]:
    """Download one public document into a random temporary file."""
    current_url = url
    session = requests.Session()
    session.trust_env = False
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            validate_remote_url(current_url)
            response = session.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=(min(10.0, DOWNLOAD_TIMEOUT), DOWNLOAD_TIMEOUT),
                verify=True,
                headers={"User-Agent": "unstructured-api/1.0"},
            )
            temp_path: Optional[str] = None
            try:
                _validate_connected_peer(response)
                if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise DownloadError("redirect response has no location")
                    if redirect_count >= MAX_REDIRECTS:
                        raise DownloadError("too many redirects")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code >= 400:
                    raise DownloadError(f"document URL returned HTTP {response.status_code}")
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > MAX_FILE_SIZE:
                            raise DownloadTooLarge("document exceeds the configured size limit")
                    except ValueError:
                        pass
                extension = _extension_for_response(current_url, response.headers)
                header_name = _filename_from_headers(response.headers)
                display_name = header_name or _safe_basename(urlsplit(current_url).path)
                if Path(display_name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    display_name = f"document{extension}"
                with tempfile.NamedTemporaryFile(mode="wb", prefix="unstructured-", suffix=extension, delete=False) as output:
                    temp_path = output.name
                    total = 0
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_FILE_SIZE:
                            raise DownloadTooLarge("document exceeds the configured size limit")
                        output.write(chunk)
                if total == 0:
                    raise DownloadError("remote document is empty")
                _check_signature(temp_path, extension)
                return temp_path, display_name, extension
            except Exception:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                raise
            finally:
                response.close()
        raise DownloadError("too many redirects")
    except requests.RequestException as exc:
        # Streamed read timeouts are wrapped in ConnectionError by requests.
        cause: Optional[BaseException] = exc
        while cause is not None:
            if isinstance(cause, (requests.Timeout, TimeoutError)):
                logger.warning("document download timed out")
                raise DownloadTimedOut("document download timed out") from exc
            cause = cause.__cause__ or cause.__context__
        logger.warning("document download failed: %s", type(exc).__name__)
        raise DownloadError("document download failed") from exc
    finally:
        session.close()


class VisionAdapter(ABC):
    @abstractmethod
    async def recognize_image(self, image_bytes: bytes, *, mime_type: str, metadata: Optional[dict] = None) -> dict:
        raise NotImplementedError


class NullVisionAdapter(VisionAdapter):
    def cache_identity(self) -> str:
        return "none"

    async def recognize_image(self, image_bytes: bytes, *, mime_type: str, metadata: Optional[dict] = None) -> dict:
        return {"status": "unconfigured", "error": "vision provider is not configured"}


class HTTPVisionAdapter(VisionAdapter):
    """Provider-neutral JSON adapter for an OpenAI-compatible vision endpoint."""

    def __init__(self, url: str, api_key: str, model: str):
        self.url = url
        self.api_key = api_key
        self.model = model

    def cache_identity(self) -> str:
        return f"http:{self.url}:{self.model}:{VISION_PROMPT_VERSION}:{RECOGNITION_CACHE_VERSION}"

    async def recognize_image(self, image_bytes: bytes, *, mime_type: str, metadata: Optional[dict] = None) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Extract useful visible text from this PDF image. Return plain text only."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"}},
            ]}],
        }
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        last_error = "vision request failed"
        for attempt in range(VISION_MAX_RETRIES + 1):
            try:
                response = await asyncio.to_thread(
                    requests.post, self.url, json=payload, headers=headers, timeout=VISION_TIMEOUT
                )
                if response.status_code >= 400:
                    last_error = f"vision API returned HTTP {response.status_code}"
                else:
                    data = response.json()
                    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return {"status": "success", "text": str(text)}
            except Exception as exc:
                last_error = type(exc).__name__
            if attempt < VISION_MAX_RETRIES:
                await asyncio.sleep(0.2 * (attempt + 1))
        return {"status": "failed", "error": last_error}


def _build_vision_adapter() -> VisionAdapter:
    if os.getenv("VISION_PROVIDER", "none").lower() in {"none", "", "disabled"}:
        return NullVisionAdapter()
    url = os.getenv("VISION_API_URL", "").strip()
    key = os.getenv("VISION_API_KEY", "")
    model = os.getenv("VISION_MODEL", "").strip()
    if not url or not key or not model:
        return NullVisionAdapter()
    return HTTPVisionAdapter(url, key, model)


def _extract_pdf_images(path: str, image_dir: str, *, max_images: int, max_image_bytes: int) -> Dict[str, Any]:
    images: List[Dict[str, Any]] = []
    errors: List[str] = []
    total_bytes = 0
    resources: Dict[int, Optional[Dict[str, Any]]] = {}
    try:
        with fitz.open(path) as document:
            Path(image_dir).mkdir(parents=True, exist_ok=True)
            for page_index in range(len(document)):
                page = document[page_index]
                page_image_number = 0
                for info in page.get_image_info(xrefs=True):
                    if len(images) >= max_images or len(images) >= MAX_PDF_IMAGES:
                        errors.append("image limit reached")
                        return {"status": "partial", "images": images, "count": len(images), "errors": errors}
                    xref = int(info.get("xref") or 0)
                    if not xref:
                        errors.append(f"page {page_index + 1}: image has no xref")
                        continue
                    try:
                        if xref not in resources:
                            resource = document.extract_image(xref)
                            resources[xref] = resource if len(resource["image"]) <= max_image_bytes else None
                        extracted = resources[xref]
                        if extracted is None:
                            errors.append(f"page {page_index + 1}: image exceeds per-image limit")
                            continue
                        image_bytes = extracted["image"]
                        ext = str(extracted.get("ext") or "bin").lower()
                        if total_bytes + len(image_bytes) > MAX_PDF_TOTAL_IMAGE_BYTES:
                            errors.append("PDF image byte budget reached")
                            return {"status": "partial", "images": images, "count": len(images), "errors": errors}
                        page_image_number += 1
                        filename = f"page-{page_index + 1}-image-{page_image_number}.{ext}"
                        target = Path(image_dir) / filename
                        target.write_bytes(image_bytes)
                        object_key = f"{STORAGE_OSS_KEY_PREFIX + '/' if STORAGE_OSS_KEY_PREFIX else ''}pdf-images/{uuid.uuid4().hex}/{filename.rsplit('.', 1)[0]}"
                        # PDFMiner coordinates include page rotation; use that frame for images too.
                        bbox = fitz.Rect(info["bbox"]) * page.rotation_matrix
                        images.append({
                            "image_id": f"page-{page_index + 1}-image-{page_image_number}",
                            "page_number": page_index + 1,
                            "bbox": [round(float(value), 3) for value in bbox],
                            "width": int(extracted.get("width", info.get("width", 0))),
                            "height": int(extracted.get("height", info.get("height", 0))),
                            "page_width": round(float(page.rect.width), 3),
                            "page_height": round(float(page.rect.height), 3),
                            "format": ext,
                            "resource_xref": xref,
                            "has_mask": bool(info.get("has-mask", False)),
                            "xres": info.get("xres"),
                            "yres": info.get("yres"),
                            "image_uri": f"images/{filename}",
                            "_object_key": object_key,
                            "_image_bytes": image_bytes,
                            "ocr_status": "not_requested",
                            "ocr_text": None,
                            "recognition_status": "PENDING",
                            "_path": str(target),
                        })
                        total_bytes += len(image_bytes)
                    except Exception as exc:
                        errors.append(f"page {page_index + 1}: image extraction failed: {type(exc).__name__}")
    except Exception as exc:
        return {"status": "failed", "images": images, "count": len(images), "errors": [type(exc).__name__]}
    return {"status": "success" if not errors else "partial", "images": images, "count": len(images), "errors": errors}


def _image_bytes(image: Dict[str, Any]) -> bytes:
    data = image.get("_image_bytes")
    if data is None:
        data = image.get("image_bytes")
    if data is not None:
        return data
    return Path(image["_path"]).read_bytes()


def _page_geometry(image: Dict[str, Any]) -> Tuple[float, float]:
    x0, y0, x1, y1 = image.get("bbox", [0, 0, 0, 0])
    page_width = float(image.get("page_width") or max(x1, 1))
    page_height = float(image.get("page_height") or max(y1, 1))
    return page_width, page_height


def _filter_image_instances(images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    resource_first: Dict[str, Dict[str, Any]] = {}
    resource_pages: Dict[str, set[int]] = {}
    for image in images:
        data = _image_bytes(image)
        image["resource_hash"] = hashlib.sha256(data).hexdigest()
        image["perceptual_hash"] = None
        image["image_width"] = image.get("width", 0)
        image["image_height"] = image.get("height", 0)
        image["image_format"] = image.get("format")
        image["display_width"] = max(0.0, float(image.get("bbox", [0, 0, 0, 0])[2]) - float(image.get("bbox", [0, 0, 0, 0])[0]))
        image["display_height"] = max(0.0, float(image.get("bbox", [0, 0, 0, 0])[3]) - float(image.get("bbox", [0, 0, 0, 0])[1]))
        page_width, page_height = _page_geometry(image)
        image["page_area_ratio"] = round((image["display_width"] * image["display_height"]) / max(page_width * page_height, 1.0), 8)
        image["filter_status"] = "PENDING"
        image["filter_reasons"] = []
        image["duplicate_of"] = None
        image["recognition_status"] = "PENDING"
        image["ocr_text"] = image.get("ocr_text")
        resource_pages.setdefault(image["resource_hash"], set()).add(int(image.get("page_number", 0)))
        if image["resource_hash"] not in resource_first:
            resource_first[image["resource_hash"]] = image
        else:
            image["duplicate_of"] = resource_first[image["resource_hash"]]["image_id"]
            image["filter_status"] = "REUSE"
            image["filter_reasons"].append("exact_duplicate_resource")
    for image in images:
        if image["filter_status"] == "REUSE":
            continue
        reasons = image["filter_reasons"]
        if IMAGE_SIZE_FILTER_ENABLED and image["image_width"] < MIN_IMAGE_WIDTH:
            reasons.append("width_below_minimum")
        if IMAGE_SIZE_FILTER_ENABLED and image["image_height"] < MIN_IMAGE_HEIGHT:
            reasons.append("height_below_minimum")
        if IMAGE_SIZE_FILTER_ENABLED and image["page_area_ratio"] < MIN_IMAGE_AREA_RATIO:
            reasons.append("display_area_below_minimum")
        if IMAGE_SIZE_FILTER_ENABLED and MIN_IMAGE_BYTES and len(_image_bytes(image)) < MIN_IMAGE_BYTES:
            reasons.append("bytes_below_minimum")
        if reasons:
            image["filter_status"] = "SKIP"
        else:
            image["filter_status"] = "CANDIDATE"
    for resource_hash, pages in resource_pages.items():
        if len(pages) < MIN_REPEATED_PAGES:
            continue
        members = [image for image in images if image["resource_hash"] == resource_hash]
        for image in members:
            page_width, page_height = _page_geometry(image)
            _, y0, _, y1 = image["bbox"]
            top = y0 / max(page_height, 1.0) <= HEADER_REGION_RATIO
            bottom = (page_height - y1) / max(page_height, 1.0) <= FOOTER_REGION_RATIO
            if top or bottom:
                if "repeated_header_or_footer" not in image["filter_reasons"]:
                    image["filter_reasons"].append("repeated_header_or_footer")
                image["filter_status"] = "SKIP"
    return images


def _upload_pdf_images(images: List[Dict[str, Any]], storage: StorageBackend, *, max_workers: int = STORAGE_UPLOAD_CONCURRENCY) -> Tuple[List[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    def resource_key(image: Dict[str, Any]) -> Tuple[str, str]:
        if image.get("resource_hash"):
            return "hash", image["resource_hash"]
        return "object", image["_object_key"]

    resources: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for image in images:
        resources.setdefault(resource_key(image), image)
    workers = max(1, min(max_workers, len(resources) or 1))

    def upload(image: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
        try:
            image_bytes = image.get("_image_bytes")
            if image_bytes is None:
                image_bytes = Path(image["_path"]).read_bytes()
            image["image_uri"] = storage.put(
                image["_object_key"],
                image_bytes,
                content_type=mimetypes.types_map.get(f".{image['format']}", "application/octet-stream"),
            )
            return image, None
        except Exception as exc:
            return image, f"{image.get('image_id', 'unknown')}: upload failed: {type(exc).__name__}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(upload, resources.values()))
    uploaded: Dict[Tuple[str, str], str] = {}
    for image, error in results:
        if error:
            errors.append(error)
        else:
            uploaded[resource_key(image)] = image["image_uri"]
    successful = []
    for image in images:
        key = resource_key(image)
        if key in uploaded:
            image["image_uri"] = uploaded[key]
            successful.append(image)
    return successful, errors


def _recognize_images(images: List[Dict[str, Any]], adapter: VisionAdapter, *, recognize_images: bool) -> List[Dict[str, Any]]:
    return _recognize_images_with_stats(images, adapter, recognize_images=recognize_images, max_recognition_images=MAX_RECOGNITION_IMAGES)[0]


def _recognize_images_with_stats(images: List[Dict[str, Any]], adapter: VisionAdapter, *, recognize_images: bool, max_recognition_images: int) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    stats = {"estimated_api_calls": 0, "recognized": 0, "reused": 0, "skipped": 0, "candidate": 0}
    if not recognize_images:
        for image in images:
            image["ocr_status"] = "disabled"
            if image.get("filter_status") in {"SKIP", "REUSE"}:
                stats["skipped"] += 1
        return images, stats

    cache_identity = getattr(adapter, "cache_identity", lambda: adapter.__class__.__name__)()
    resource_results: Dict[str, Dict[str, Any]] = {}
    calls = 0

    async def recognize_all() -> None:
        nonlocal calls
        for image in images:
            if image.get("filter_status") == "SKIP":
                image["recognition_status"] = "SKIP"
                stats["skipped"] += 1
                continue
            if image.get("filter_status") == "REUSE" and image.get("duplicate_of"):
                source = next((item for item in images if item["image_id"] == image["duplicate_of"]), None)
                if source:
                    if source.get("ocr_text"):
                        image["ocr_text"] = source["ocr_text"]
                        image["ocr_status"] = source["ocr_status"]
                        image["recognition_status"] = "REUSE"
                        stats["reused"] += 1
                        continue
                    image["filter_status"] = "CANDIDATE"
            stats["candidate"] += 1
            resource_hash = image.get("resource_hash")
            cache_key = (cache_identity, resource_hash or hashlib.sha256(_image_bytes(image)).hexdigest())
            with _recognition_cache_lock:
                cached = _recognition_cache.get(cache_key)
            if cached is not None:
                image["ocr_text"] = cached["text"]
                image["recognition_status"] = "REUSED"
                image["ocr_status"] = "success"
                stats["reused"] += 1
                continue
            if resource_hash in resource_results:
                image["ocr_text"] = resource_results[resource_hash]["text"]
                image["recognition_status"] = "REUSE"
                image["ocr_status"] = "success"
                stats["reused"] += 1
                continue
            if calls >= max_recognition_images:
                image["recognition_status"] = "BUDGET_EXCEEDED"
                image["filter_reasons"] = image.get("filter_reasons", []) + ["recognition_budget_exceeded"]
                stats["skipped"] += 1
                continue
            try:
                image_bytes = _image_bytes(image)
                calls += 1
                result = await adapter.recognize_image(
                    image_bytes, mime_type=mimetypes.types_map.get(f".{image['format']}", "application/octet-stream"),
                    metadata={"image_id": image["image_id"], "page_number": image["page_number"]},
                )
                image["ocr_status"] = result.get("status", "failed")
                image["ocr_text"] = result.get("text")
                image["recognition_status"] = "RECOGNIZED" if result.get("status") == "success" else "FAILED"
                if result.get("error"):
                    image["ocr_error"] = result["error"]
                if result.get("status") == "success" and image.get("resource_hash"):
                    resource_results[image["resource_hash"]] = {"text": image.get("ocr_text")}
                    with _recognition_cache_lock:
                        if len(_recognition_cache) >= RECOGNITION_CACHE_MAX_ENTRIES:
                            _recognition_cache.pop(next(iter(_recognition_cache)))
                        _recognition_cache[cache_key] = {"text": image.get("ocr_text")}
                    stats["recognized"] += 1
            except Exception as exc:
                image["ocr_status"] = "failed"
                image["recognition_status"] = "FAILED"
                image["ocr_error"] = type(exc).__name__

    asyncio.run(recognize_all())
    stats["estimated_api_calls"] = calls
    return images, stats


def _markdown_for_pdf(elements: List[Any], images: List[Dict[str, Any]]) -> str:
    """Preserve Unstructured text order and insert images using page coordinates."""
    def image_position(image: Dict[str, Any]) -> Tuple[int, float, float]:
        return (
            image["page_number"],
            image["bbox"][1] / max(float(image["page_height"]), 1.0),
            image["bbox"][0] / max(float(image["page_width"]), 1.0),
        )

    def image_markdown(image: Dict[str, Any]) -> str:
        content = f"![PDF 第 {image['page_number']} 页图片]({image['image_uri']})"
        if image.get("ocr_status") == "success" and image.get("ocr_text"):
            quote = "\n".join("> " + line for line in ("图片识别文本：" + image["ocr_text"].strip()).splitlines())
            content += "\n\n" + quote
        return content

    pending = sorted(images, key=image_position)
    image_index = 0
    page_number = 1
    paragraphs: List[str] = []
    for element in elements:
        text = str(getattr(element, "text", "") or "")
        if not text:
            continue
        metadata = getattr(element, "metadata", None)
        page_number = getattr(metadata, "page_number", None) or page_number
        coordinates = getattr(metadata, "coordinates", None)
        anchor: Optional[Tuple[float, float]] = None
        if image_index < len(pending) and coordinates and coordinates.system and coordinates.points:
            points = [coordinates.system.convert_to_relative(x, y) for x, y in coordinates.points]
            # Unstructured relative coordinates use a bottom-left origin.
            anchor = (1.0 - max(y for _, y in points), min(x for x, _ in points))
        while image_index < len(pending):
            image_page, image_y, image_x = image_position(pending[image_index])
            if image_page > page_number:
                break
            if image_page == page_number and (anchor is None or (image_y, image_x) > anchor):
                break
            paragraphs.append(image_markdown(pending[image_index]))
            image_index += 1
        paragraphs.append(text)
    paragraphs.extend(image_markdown(image) for image in pending[image_index:])
    return "\n\n".join(paragraphs)


def _parse_document(path: str, display_name: str, extension: str, *, max_pages: int = 100, extract_images: bool = True, recognize_images: bool = False, max_images: int = 100, max_image_bytes: int = 10 * 1024 * 1024, filter_images: bool = True, max_recognition_images: int = MAX_RECOGNITION_IMAGES) -> Dict[str, Any]:
    warnings: List[str] = []
    with tempfile.TemporaryDirectory(prefix="unstructured-parse-") as work_dir:
        if extension in PDF_EXTENSIONS:
            try:
                with fitz.open(path) as document:
                    total_pages = len(document)
                    parsed_pages = min(total_pages, max_pages)
                    if parsed_pages < total_pages:
                        document.select(range(parsed_pages))
                        path = str(Path(work_dir) / "pages.pdf")
                        document.save(path, garbage=3)
                        warnings.append(f"PDF contains {total_pages} pages; only the first {parsed_pages} pages were parsed")
            except (fitz.FileDataError, RuntimeError, ValueError) as exc:
                raise DownloadError("PDF pages could not be read; the file may be damaged or password-protected") from exc
        if extension in IMAGE_EXTENSIONS:
            raise UnsupportedDocument(
                "image parsing is disabled in the default CPU-only non-OCR mode; install and explicitly enable an image strategy separately"
            )
        kwargs: Dict[str, Any] = {
            "filename": path,
            "metadata_filename": display_name,
            # Coordinates are internal input for placing images, never response data.
            "include_metadata": extension in PDF_EXTENSIONS and extract_images,
        }
        if extension in PDF_EXTENSIONS:
            kwargs["strategy"] = "fast"
        try:
            elements = partition(**kwargs)
        except Exception as exc:
            logger.warning("document text parsing failed for %s: %s", extension, type(exc).__name__)
            raise DownloadError("document text parsing failed; the file may be damaged or missing a format dependency") from exc
        text_parts = [str(element.text) for element in elements if getattr(element, "text", None)]
        merged_text = "\n\n".join(text_parts)
        images: List[Dict[str, Any]] = []
        if extension in PDF_EXTENSIONS and extract_images:
            image_dir = str(Path(work_dir) / "images")
            extracted_images = _extract_pdf_images(
                path,
                image_dir,
                max_images=max_images,
                max_image_bytes=max_image_bytes,
            )
            warnings.extend(extracted_images["errors"])
            images = extracted_images["images"]
            if images:
                if filter_images:
                    images = _filter_image_instances(images)
                else:
                    for image in images:
                        image["filter_status"] = "CANDIDATE"
                        image["filter_reasons"] = []
                        image["resource_hash"] = hashlib.sha256(_image_bytes(image)).hexdigest()
                        image["perceptual_hash"] = None
                        image["image_width"] = image.get("width", 0)
                        image["image_height"] = image.get("height", 0)
                        image["image_format"] = image.get("format")
                        image["display_width"] = max(0.0, float(image.get("bbox", [0, 0, 0, 0])[2]) - float(image.get("bbox", [0, 0, 0, 0])[0]))
                        image["display_height"] = max(0.0, float(image.get("bbox", [0, 0, 0, 0])[3]) - float(image.get("bbox", [0, 0, 0, 0])[1]))
                        page_width, page_height = _page_geometry(image)
                        image["page_area_ratio"] = round((image["display_width"] * image["display_height"]) / max(page_width * page_height, 1.0), 8)
                        image["duplicate_of"] = None
                        image["recognition_status"] = "PENDING"
                images, upload_errors = _upload_pdf_images(images, _build_storage_backend())
                warnings.extend(upload_errors)
            images, _ = _recognize_images_with_stats(
                images,
                _build_vision_adapter(),
                recognize_images=recognize_images,
                max_recognition_images=max_recognition_images,
            )
            for image in images:
                if image.get("ocr_status") in {"failed", "unconfigured"}:
                    warnings.append(f"{image['image_id']}: {image.get('ocr_error', 'recognition failed')}")
                elif image.get("recognition_status") == "BUDGET_EXCEEDED":
                    warnings.append("image recognition budget reached; some images were not recognized")
        if extract_images:
            result = {"markdown": (
                _markdown_for_pdf(elements, images)
                if extension in PDF_EXTENSIONS
                else merged_text.strip()
            )}
        else:
            result = {"text": merged_text}
        if warnings:
            result["warnings"] = list(dict.fromkeys(warnings))
        return result


app = FastAPI(title=SERVICE_NAME, version="1.0.0")


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.post(
    "/api/v1/parse",
    description="Returns only markdown (with image object keys and available OCR text), or text when extract_images is false. Warnings are included only for truncated or incomplete results.",
    response_model=MarkdownResponse | TextResponse,
    response_model_exclude_none=True,
)
async def parse_document(request: ParseRequest) -> Dict[str, Any]:
    temp_path: Optional[str] = None
    started = time.perf_counter()
    try:
        temp_path, display_name, extension = await run_in_threadpool(_download_document, request.url)
        result = await run_in_threadpool(
            _parse_document,
            temp_path,
            display_name,
            extension,
            max_pages=request.max_pages,
            extract_images=request.extract_images,
            recognize_images=request.recognize_images,
            max_images=request.max_images,
            max_image_bytes=request.max_image_bytes,
            filter_images=request.filter_images,
            max_recognition_images=request.max_recognition_images,
        )
        logger.info("parsed %s type=%s duration_ms=%s warnings=%s", display_name, extension, round((time.perf_counter() - started) * 1000, 2), len(result.get("warnings", [])))
        return result
    except DownloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("unexpected parse error: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="document processing failed") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("temporary file cleanup failed")
        logger.debug("request completed duration_ms=%s", round((time.perf_counter() - started) * 1000, 2))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=_env_int("PORT", DEFAULT_PORT),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
