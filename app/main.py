from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app.errors import DownloadError, DownloadTimedOut, DownloadTooLarge, ModelUnavailable, UnsupportedDocument, URLRejected
from app.storage import LocalStorageBackend, build_storage_backend

SERVICE_NAME = "huahuo-parse"
DEFAULT_PORT = 4763
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv", ".txt", ".md", ".markdown", ".html", ".htm", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
TEXT_EXTENSIONS = {".csv", ".txt", ".md", ".markdown", ".html", ".htm"}
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", "512")) * 1024 * 1024
DOWNLOAD_TIMEOUT = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "120"))
MAX_REDIRECTS = int(os.getenv("MAX_REDIRECTS", "3"))
logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
logger.propagate = False
# urllib3 DEBUG messages include the full request target, including OSS signatures.
logging.getLogger("urllib3").setLevel(logging.WARNING)


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_url: str = Field(..., min_length=1, description="Direct download HTTP(S) URL, including its original signature")
    file_type: str = Field(..., description="Declared file extension, for example pdf, docx, xlsx, pptx or png")

    @field_validator("file_url")
    @classmethod
    def validate_url(cls, value):
        # Signed URLs are opaque: never decode, normalize, strip or reorder them.
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("file_url contains whitespace or control characters")
        return value

    @field_validator("file_type")
    @classmethod
    def validate_file_type(cls, value):
        extension = "." + value.lower().lstrip(".")
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError("unsupported file_type")
        return extension.lstrip(".")


class MarkdownResponse(BaseModel):
    markdown: str
    metadata: dict
    warnings: list[str] | None = None


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast


def _resolved_addresses(host: str, port: int) -> list[str]:
    resolver = os.getenv("DOWNLOAD_DNS_OVER_HTTPS", "")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if resolver:
            # Optional on development machines with a fake-IP VPN. Production uses system DNS.
            with requests.Session() as dns:
                dns.trust_env = False
                response = dns.get(resolver, params={"name": host, "type": "A"}, headers={"Accept": "application/dns-json"}, timeout=10)
                response.raise_for_status()
                addresses = [answer["data"] for answer in response.json().get("Answer", []) if answer.get("type") == 1]
        else:
            addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise URLRejected("URL resolves to a private or reserved network address")
    return addresses


class PublicAddressAdapter(requests.adapters.HTTPAdapter):
    """Pin the validated address, while retaining the original Host header and TLS SNI."""

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, pool = super().build_connection_pool_key_attributes(request, verify, cert)
        hostname = urlsplit(request.url).hostname
        address = _resolved_addresses(hostname, host["port"] or (443 if host["scheme"] == "https" else 80))[0]
        request.headers["Host"] = urlsplit(request.url).netloc
        host["host"] = address
        if host["scheme"] == "https":
            pool.update(server_hostname=hostname, assert_hostname=hostname)
        return host, pool


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
        addresses = _resolved_addresses(parsed.hostname, port)
    except socket.gaierror as exc:
        raise URLRejected("URL host cannot be resolved") from exc
    if not addresses:
        raise URLRejected("URL host has no usable address")
    for address in addresses:
        try:
            public = _is_public_ip(address)
        except ValueError as exc:
            raise URLRejected("URL host resolved to an invalid address") from exc
        if not public:
            raise URLRejected("URL resolves to a private or reserved network address")


def _safe_basename(name: str) -> str:
    name = unquote(name).replace("\\", "/")
    return Path(name).name or "document"


def _filename_from_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> str | None:
    value = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^;\"]+)", value, flags=re.IGNORECASE)
    return _safe_basename(match.group(1).strip()) if match else None


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


def _check_signature(path: str, extension: str) -> None:
    from PIL import Image

    with open(path, "rb") as stream:
        signature = stream.read(16)
    if not signature:
        raise DownloadError("remote document is empty")
    mismatch = False
    if extension == ".pdf":
        mismatch = not signature.startswith(b"%PDF-")
    elif extension in {".docx", ".xlsx", ".pptx"}:
        root = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[extension]
        try:
            with zipfile.ZipFile(path) as archive:
                mismatch = root not in archive.namelist() or "[Content_Types].xml" not in archive.namelist()
        except (OSError, zipfile.BadZipFile):
            mismatch = True
    elif extension in {".doc", ".xls", ".ppt"}:
        if not signature.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            mismatch = True
        else:
            import olefile
            with olefile.OleFileIO(path) as compound:
                streams = {".doc": ["WordDocument"], ".xls": ["Workbook", "Book"], ".ppt": ["PowerPoint Document"]}[extension]
                mismatch = not any(compound.exists(stream) for stream in streams)
    elif extension in IMAGE_EXTENSIONS:
        try:
            with Image.open(path) as image:
                expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".tiff": "TIFF", ".tif": "TIFF", ".bmp": "BMP"}[extension]
                mismatch = image.format != expected
                image.verify()
        except Exception:
            mismatch = True
    elif extension in TEXT_EXTENSIONS:
        try:
            with open(path, encoding="utf-8-sig") as stream:
                sample = stream.read(8192)
            mismatch = "\x00" in sample or signature.startswith((b"%PDF", b"PK", bytes.fromhex("D0CF11E0")))
        except UnicodeDecodeError:
            mismatch = True
    if mismatch:
        raise UnsupportedDocument("declared file_type does not match the downloaded file content")


def _download_document(url: str, file_type: str) -> tuple[str, str, str]:
    """Bounded streaming transport; parser choice follows the declaration, not the URL suffix."""
    extension = "." + file_type.lower().lstrip(".")
    if extension not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocument("unsupported file_type")
    current_url, temp_path = url, None
    session = requests.Session()
    session.trust_env = False
    session.mount("http://", PublicAddressAdapter())
    session.mount("https://", PublicAddressAdapter())
    started = time.monotonic()
    succeeded = False
    try:
        for redirects in range(MAX_REDIRECTS + 1):
            validate_remote_url(current_url)
            prepared = session.prepare_request(requests.Request("GET", current_url, headers={"User-Agent": "huahuo-parse/2.0"}))
            prepared.url = current_url  # prepare_url otherwise normalizes signed path escapes.
            response = session.send(prepared, stream=True, allow_redirects=False, timeout=(min(10, DOWNLOAD_TIMEOUT), DOWNLOAD_TIMEOUT), verify=True)
            try:
                _validate_connected_peer(response)
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirects == MAX_REDIRECTS or not response.headers.get("Location"):
                        raise DownloadError("document redirect limit reached or location missing")
                    current_url = urljoin(current_url, response.headers["Location"])
                    continue
                if response.status_code >= 400:
                    reason = "; link may have expired" if response.status_code in {401, 403} else ""
                    raise DownloadError(f"document URL returned HTTP {response.status_code}{reason}")
                try:
                    length = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > MAX_FILE_SIZE:
                    raise DownloadTooLarge("document exceeds the configured size limit")
                display_name = _filename_from_headers(response.headers) or _safe_basename(urlsplit(current_url).path)
                with tempfile.NamedTemporaryFile(mode="wb", prefix="huahuo-download-", suffix=extension, delete=False) as output:
                    temp_path = output.name
                    total = 0
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                            raise DownloadTimedOut("document download timed out")
                        total += len(chunk)
                        if total > MAX_FILE_SIZE:
                            raise DownloadTooLarge("document exceeds the configured size limit")
                        output.write(chunk)
                _check_signature(temp_path, extension)
                succeeded = True
                return temp_path, display_name, extension
            finally:
                response.close()
        raise DownloadError("too many redirects")
    except requests.RequestException as exc:
        cause = exc
        while cause is not None:
            if isinstance(cause, (requests.Timeout, TimeoutError)):
                raise DownloadTimedOut("document download timed out") from exc
            cause = cause.__cause__ or cause.__context__
        raise DownloadError("document download failed") from exc
    finally:
        if temp_path and not succeeded:
            Path(temp_path).unlink(missing_ok=True)
        session.close()


_build_storage_backend = build_storage_backend


def _parse_document(path, display_name, extension, *, document_id=None):
    from app.parsing import parse_document
    return parse_document(path, display_name, extension, _build_storage_backend(), document_id or uuid.uuid4().hex)


@asynccontextmanager
async def lifespan(app):
    from app.vision import engine
    app.state.document_slots = asyncio.Semaphore(1)
    if os.getenv("PRELOAD_MODEL", "true").lower() in {"true", "1", "yes"}:
        try:
            await run_in_threadpool(engine.initialize)
        except ModelUnavailable as exc:
            logger.error("service startup failed: %s", exc)
            raise
    yield


app = FastAPI(title=SERVICE_NAME, version="2.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    from app.vision import backend_name, engine
    return {"status": "ok", "service": SERVICE_NAME, "inference_backend": backend_name(), "model_loaded": engine.pipeline is not None, "visual_calls": engine.calls}


@app.get("/artifacts/{key:path}")
async def artifact(key: str):
    storage = _build_storage_backend()
    if not isinstance(storage, LocalStorageBackend):
        raise HTTPException(404, "local artifact storage is disabled")
    try:
        path = storage.resolve(key)
    except ValueError:
        raise HTTPException(404, "artifact not found")
    if not path.is_file() or not key.startswith("documents/"):
        raise HTTPException(404, "artifact not found")
    media_type = "text/markdown; charset=utf-8" if key.endswith("/markdown") else "image/png"
    return FileResponse(path, media_type=media_type)


@app.post("/api/v1/parse", response_model=MarkdownResponse, response_model_exclude_none=True,
          description="Fully parse every page into UTF-8 Markdown with native Office fields and source metadata.")
async def parse_document(request: ParseRequest):
    temp_path = None
    started = time.perf_counter()
    document_id = uuid.uuid4().hex
    try:
        async with app.state.document_slots:
            temp_path, display_name, extension = await run_in_threadpool(_download_document, request.file_url, request.file_type)
            result = await run_in_threadpool(_parse_document, temp_path, display_name, extension, document_id=document_id)
        logger.info("parsed document_id=%s type=%s elapsed_seconds=%.3f status=%s", document_id, extension, time.perf_counter() - started, result["metadata"]["status"])
        return result
    except DownloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("document processing failed document_id=%s error=%s", document_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="document processing failed") from exc
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", str(DEFAULT_PORT))), workers=1, log_level=os.getenv("LOG_LEVEL", "info").lower())
