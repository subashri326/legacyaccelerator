"""Object storage abstraction for raw exports, generated code, Parquet staging, and evidence."""

from __future__ import annotations

import hashlib
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path

from accelerator.config.settings import get_settings


class ArtifactStore(ABC):
    """Abstract S3-compatible / ADLS / local artifact repository."""

    @abstractmethod
    def put_bytes(self, key: str, content: bytes) -> str:
        """Persist bytes under logical key and return URI."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Read bytes for logical key."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check whether key exists."""

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """List logical keys under prefix."""

    def put_text(self, key: str, text: str) -> str:
        return self.put_bytes(key, text.encode("utf-8"))

    def get_text(self, key: str) -> str:
        return self.get_bytes(key).decode("utf-8")

    @staticmethod
    def sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()


class LocalArtifactStore(ArtifactStore):
    """Local filesystem artifact store (default for dev, tests, offline assessment)."""

    def __init__(self, root: Path | None = None) -> None:
        self._dir_lock = threading.Lock()
        self.root = (root or get_settings().storage_local_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        clean = key.lstrip("/").replace("..", "__")
        target = (self.root / clean).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError(f"Unsafe artifact key: {key}")
        return target

    def resolve_path(self, key: str) -> Path:
        return self._resolve(key)

    def put_bytes(self, key: str, content: bytes) -> str:
        path = self._resolve(key)
        with self._dir_lock:
            for attempt in range(3):
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        path.write_bytes(content)
        return f"local://{key.lstrip('/')}"

    def get_bytes(self, key: str) -> bytes:
        clean = key.removeprefix("local://")
        return self._resolve(clean).read_bytes()

    def exists(self, key: str) -> bool:
        clean = key.removeprefix("local://")
        return self._resolve(clean).exists()

    def list_prefix(self, prefix: str) -> list[str]:
        clean = prefix.removeprefix("local://").lstrip("/")
        base = self._resolve(clean)
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.root)).replace("\\", "/")
            for p in sorted(base.rglob("*"))
            if p.is_file()
        ]


class S3ArtifactStore(ArtifactStore):
    """S3 / MinIO artifact store via boto3."""

    def __init__(self, bucket: str | None = None, endpoint_url: str | None = None) -> None:
        import boto3  # optional dependency

        settings = get_settings()
        self.bucket = bucket or settings.s3_bucket
        self.client = boto3.client("s3", endpoint_url=endpoint_url or settings.s3_endpoint_url)

    def put_bytes(self, key: str, content: bytes) -> str:
        clean = key.lstrip("/")
        self.client.put_object(Bucket=self.bucket, Key=clean, Body=content)
        return f"s3://{self.bucket}/{clean}"

    def get_bytes(self, key: str) -> bytes:
        clean = key.removeprefix(f"s3://{self.bucket}/").lstrip("/")
        resp = self.client.get_object(Bucket=self.bucket, Key=clean)
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        clean = key.removeprefix(f"s3://{self.bucket}/").lstrip("/")
        try:
            self.client.head_object(Bucket=self.bucket, Key=clean)
            return True
        except Exception:  # botocore ClientError comes from optional boto3 dependency
            return False

    def list_prefix(self, prefix: str) -> list[str]:
        clean = prefix.lstrip("/")
        paginator = self.client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=clean):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys


class ADLSArtifactStore(ArtifactStore):
    """Azure Data Lake Storage Gen2 / OneLake artifact store."""

    def __init__(self, account_url: str | None = None, container: str | None = None) -> None:
        from azure.storage.blob import BlobServiceClient  # optional dependency

        settings = get_settings()
        self.account_url = account_url or settings.adls_account_url
        self.container = container or settings.adls_container
        self.service = BlobServiceClient(account_url=self.account_url)
        self.container_client = self.service.get_container_client(self.container)

    def put_bytes(self, key: str, content: bytes) -> str:
        clean = key.lstrip("/")
        self.container_client.upload_blob(name=clean, data=content, overwrite=True)
        return f"abfs://{self.container}/{clean}"

    def get_bytes(self, key: str) -> bytes:
        clean = key.removeprefix(f"abfs://{self.container}/").lstrip("/")
        return self.container_client.download_blob(clean).readall()

    def exists(self, key: str) -> bool:
        clean = key.removeprefix(f"abfs://{self.container}/").lstrip("/")
        return self.container_client.get_blob_client(clean).exists()

    def list_prefix(self, prefix: str) -> list[str]:
        clean = prefix.lstrip("/")
        return [b.name for b in self.container_client.list_blobs(name_starts_with=clean)]


def get_artifact_store() -> ArtifactStore:
    backend = get_settings().storage_backend
    if backend == "s3":
        return S3ArtifactStore()
    if backend == "adls":
        return ADLSArtifactStore()
    return LocalArtifactStore()
