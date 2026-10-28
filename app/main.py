from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlsplit

# Keep the library's commercial offering notice out of service logs.
os.environ.setdefault("UNSTRUCTURED_DISABLE_NOTICE", "1")

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from unstructured.partition.auto import partition

SERVICE_NAME = "unstructured-api"
DEFAULT_PORT = 8000
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


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE_MB", 50) * 1024 * 1024
DOWNLOAD_TIMEOUT = _env_float("DOWNLOAD_TIMEOUT_SECONDS", DEFAULT_DOWNLOAD_TIMEOUT)
MAX_REDIRECTS = _env_int("MAX_REDIRECTS", DEFAULT_MAX_REDIRECTS)


class ParseRequest(BaseModel):
    url: str = Field(..., min_length=1, description="HTTP or HTTPS URL of a document")
    include_metadata: bool = True
    include_elements: bool = True

    @field_validator("url")
    @classmethod
    def validate_url_shape(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("url must not be empty")
        return value


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
            try:
                response = session.get(
                    current_url,
                    stream=True,
                    allow_redirects=False,
                    timeout=(min(10.0, DOWNLOAD_TIMEOUT), DOWNLOAD_TIMEOUT),
                    verify=True,
                    headers={"User-Agent": "unstructured-api/1.0"},
                )
            except requests.Timeout as exc:
                raise DownloadTimedOut("document download timed out") from exc
            except requests.RequestException as exc:
                raise DownloadError("document URL could not be fetched") from exc
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
    finally:
        session.close()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if key != "file_directory"}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _element_metadata(element: Any, include_metadata: bool) -> Dict[str, Any]:
    if not include_metadata:
        return {}
    metadata = getattr(element, "metadata", None)
    if metadata is None:
        return {}
    try:
        raw = metadata.to_dict() if hasattr(metadata, "to_dict") else dict(metadata)
    except Exception:
        raw = getattr(metadata, "__dict__", {})
    if not isinstance(raw, dict):
        return {}
    raw.pop("file_directory", None)
    raw.pop("filename", None)
    raw.pop("metadata_file_path", None)
    return _json_safe(raw)


def _parse_document(path: str, display_name: str, extension: str, include_metadata: bool, include_elements: bool) -> Dict[str, Any]:
    started = time.perf_counter()
    if extension in IMAGE_EXTENSIONS:
        raise UnsupportedDocument(
            "image parsing is disabled in the default CPU-only non-OCR mode; install and explicitly enable an image strategy separately"
        )
    kwargs: Dict[str, Any] = {"filename": path, "metadata_filename": display_name}
    strategy = "native"
    if extension in PDF_EXTENSIONS:
        kwargs["strategy"] = "fast"
        strategy = "fast"
    try:
        elements = partition(**kwargs)
    except UnsupportedDocument:
        raise
    except Exception as exc:
        logger.warning("document parsing failed for %s: %s", extension, type(exc).__name__)
        raise DownloadError("document parsing failed; the file may be damaged or missing a format dependency") from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    text_parts: List[str] = []
    serialized: List[Dict[str, Any]] = []
    for element in elements:
        text = str(getattr(element, "text", "") or "")
        if text:
            text_parts.append(text)
        if include_elements:
            serialized.append({
                "category": getattr(element, "category", element.__class__.__name__),
                "text": text,
                "metadata": _element_metadata(element, include_metadata),
            })
    merged_text = "\n\n".join(text_parts)
    return {
        "success": True,
        "filename": display_name,
        "file_type": extension.lstrip("."),
        "strategy": strategy,
        "duration_ms": elapsed_ms,
        "element_count": len(elements),
        "text_length": len(merged_text),
        "text": merged_text,
        "elements": serialized,
    }


app = FastAPI(title=SERVICE_NAME, version="1.0.0")


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/api/v1/parse")
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
            request.include_metadata,
            request.include_elements,
        )
        logger.info("parsed %s type=%s elements=%s duration_ms=%s", display_name, extension, result["element_count"], result["duration_ms"])
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
