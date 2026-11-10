from __future__ import annotations

import hashlib
import os
from typing import Any

from app.errors import DownloadError


class DocumentResult:
    """Publish the final Markdown only after every referenced asset is ready."""

    def __init__(self, document_id: str, storage: Any):
        self.document_id = document_id
        self.storage = storage
        prefix = os.getenv("STORAGE_ALIYUN_OSS_KEY_PREFIX", "").strip("/") if os.getenv("STORAGE_DRIVER") == "aliyun_oss" else ""
        self.namespace = "/".join(part for part in (prefix, "documents", document_id) if part)
        self.parts: list[str] = []
        self.metadata: dict = {"document_id": document_id, "sources": [], "assets": [], "issues": []}
        self._assets: dict[str, dict] = {}

    def issue(self, code: str, location: dict, reason: str):
        self.metadata["issues"].append({"code": code, "location": location, "reason": reason})

    def asset(self, data: bytes, *, content_type: str, location: dict, unique_reference: bool = False) -> str | None:
        """Return a URI, an empty string for an omitted repeat, or None on failure."""
        digest = hashlib.sha256(data).hexdigest()
        if existing := self._assets.get(digest):
            if unique_reference:
                existing.setdefault("locations", [existing["location"]]).append(location)
                return ""
            return existing["uri"]
        key = f"{self.namespace}/assets/{digest}"
        try:
            key = self.storage.put(key, data, content_type=content_type)
        except Exception as exc:
            self.issue("artifact_upload_failed", location, f"artifact upload failed ({type(exc).__name__})")
            return None
        # Existing OSS consumers resolve object keys; local artifacts also have an HTTP endpoint.
        base = os.getenv("STORAGE_PUBLIC_BASE_URL", "").rstrip("/")
        uri = f"{base}/{key}" if base else key
        asset = {
            "object_key": key, "uri": uri, "content_type": content_type,
            "sha256": digest, "location": location,
            **({"download_path": f"/artifacts/{key}"} if hasattr(self.storage, "resolve") else {}),
        }
        self._assets[digest] = asset
        self.metadata["assets"].append(asset)
        return uri

    def finish(self) -> dict:
        markdown = "\n\n".join(part.strip() for part in self.parts if part.strip()).strip() + "\n"
        key = f"{self.namespace}/markdown"
        try:
            self.metadata["markdown_object_key"] = self.storage.put(
                key, markdown.encode("utf-8"), content_type="text/markdown; charset=utf-8"
            )
        except Exception as exc:
            raise DownloadError("Markdown artifact could not be committed") from exc
        result = {"markdown": markdown, "metadata": self.metadata}
        self.metadata["status"] = "partial" if self.metadata["issues"] else "complete"
        if self.metadata["issues"]:
            result["warnings"] = [
                f"{item['location']}: {item['reason']}" for item in self.metadata["issues"]
            ]
        return result
