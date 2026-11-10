"""The existing extensionless local/OSS object storage contract."""
from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath


class StorageBackend(ABC):
    @abstractmethod
    def put(self, path: str, data: bytes, *, content_type: str) -> str:
        raise NotImplementedError


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def resolve(self, key: str) -> Path:
        path = PurePosixPath(key)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("invalid storage key")
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("invalid storage key")
        return target

    def put(self, path: str, data: bytes, *, content_type: str) -> str:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
        return path


class AliyunOSSStorageBackend(StorageBackend):
    def __init__(self, endpoint: str, bucket: str, access_key_id: str, access_key_secret: str):
        import oss2
        self.bucket = oss2.Bucket(
            oss2.Auth(access_key_id, access_key_secret), endpoint, bucket,
            connect_timeout=float(os.getenv("STORAGE_ALIYUN_OSS_TIMEOUT_MS", "120000")) / 1000,
        )

    def put(self, path: str, data: bytes, *, content_type: str) -> str:
        result = self.bucket.put_object(path, data, headers={"Content-Type": content_type})
        if result.status >= 300:
            raise OSError("artifact upload failed")
        return path


def build_storage_backend() -> StorageBackend:
    driver = os.getenv("STORAGE_DRIVER", "local").lower()
    if driver == "local":
        return LocalStorageBackend(os.getenv("STORAGE_LOCAL_ROOT", "./var/storage"))
    if driver != "aliyun_oss":
        raise ValueError("unsupported STORAGE_DRIVER")
    names = ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "ACCESS_KEY_SECRET")
    values = [os.getenv("STORAGE_ALIYUN_OSS_" + name, "") for name in names]
    if not all(values):
        raise ValueError("Aliyun OSS storage is not fully configured")
    return AliyunOSSStorageBackend(*values)
