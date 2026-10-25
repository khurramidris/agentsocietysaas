"""Blob storage for dataset packages: local filesystem or S3-compatible.

Uploads and downloads go straight between the client and the store via signed
URLs (S3 presigned URLs, or HMAC-signed URLs to this service for the local
backend), matching the multipart flow the upstream dataset skills implement.
The backend's multipart upload id is returned by :meth:`BlobStore.begin_upload`
and stored with the upload record, so any control-plane replica can finish it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote, urlencode

from .security import sign, verify_signature
from .settings import Settings

URL_TTL_S = 3600


class BlobStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def begin_upload(self, key: str) -> str | None:
        """Start a multipart upload; returns the backend's upload id (if it has one)."""
        return None

    async def part_upload_url(self, key: str, part_number: int, upload_id: str | None) -> str:
        raise NotImplementedError

    async def complete_parts(self, key: str, parts: int, upload_id: str | None) -> int:
        """Assemble the parts into the object at ``key``; returns its size in bytes."""
        raise NotImplementedError

    async def download_url(self, key: str, filename: str) -> str:
        raise NotImplementedError

    @contextlib.asynccontextmanager
    async def local_copy(self, key: str) -> AsyncIterator[Path]:
        """A local file with the object's content, valid inside the ``async with``."""
        raise NotImplementedError
        yield  # pragma: no cover

    async def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalBlobStore(BlobStore):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.root = (Path(settings.data_dir) / "blobs").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise ValueError("invalid blob key")
        return target

    def _signed(self, path: str, purpose: str, payload: str, extra: dict[str, str] | None = None) -> str:
        exp = int(time.time()) + URL_TTL_S
        query = {"exp": str(exp), "sig": sign(self.settings, purpose, payload, exp), **(extra or {})}
        return f"{self.settings.public_base_url}{path}?{urlencode(query)}"

    async def part_upload_url(self, key: str, part_number: int, upload_id: str | None) -> str:
        payload = f"{key}#{part_number}"
        return self._signed(f"/api/v1/blobs/upload/{quote(key)}", "blob-put", payload, {"part": str(part_number)})

    def verify_part(self, key: str, part_number: int, exp: int, sig: str) -> bool:
        return verify_signature(self.settings, "blob-put", f"{key}#{part_number}", exp, sig)

    def part_path(self, key: str, part_number: int) -> Path:
        parts_dir = self.path(key + ".parts")
        parts_dir.mkdir(parents=True, exist_ok=True)
        return parts_dir / f"{part_number:06d}"

    async def complete_parts(self, key: str, parts: int, upload_id: str | None) -> int:
        def _combine() -> int:
            parts_dir = self.path(key + ".parts")
            target = self.path(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                for n in range(1, parts + 1):
                    part = parts_dir / f"{n:06d}"
                    if not part.exists():
                        raise FileNotFoundError(f"missing part {n}")
                    with open(part, "rb") as src:
                        shutil.copyfileobj(src, out)
            shutil.rmtree(parts_dir, ignore_errors=True)
            return target.stat().st_size

        return await asyncio.to_thread(_combine)

    async def download_url(self, key: str, filename: str) -> str:
        return self._signed(f"/api/v1/blobs/download/{quote(key)}", "blob-get", key, {"filename": filename})

    def verify_download(self, key: str, exp: int, sig: str) -> bool:
        return verify_signature(self.settings, "blob-get", key, exp, sig)

    @contextlib.asynccontextmanager
    async def local_copy(self, key: str) -> AsyncIterator[Path]:
        yield self.path(key)

    async def delete(self, key: str) -> None:
        shutil.rmtree(self.path(key + ".parts"), ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path(key))


class S3BlobStore(BlobStore):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        import boto3  # optional dependency: controlplane[s3]

        self.bucket = settings.s3_bucket
        self.prefix = settings.s3_prefix
        self.s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint_url or None, region_name=settings.s3_region or None)

    def _key(self, key: str) -> str:
        return self.prefix + key

    async def begin_upload(self, key: str) -> str | None:
        resp = await asyncio.to_thread(self.s3.create_multipart_upload, Bucket=self.bucket, Key=self._key(key))
        return resp["UploadId"]

    async def part_upload_url(self, key: str, part_number: int, upload_id: str | None) -> str:
        return await asyncio.to_thread(
            self.s3.generate_presigned_url,
            "upload_part",
            Params={"Bucket": self.bucket, "Key": self._key(key), "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=URL_TTL_S,
        )

    async def complete_parts(self, key: str, parts: int, upload_id: str | None) -> int:
        listed = await asyncio.to_thread(self.s3.list_parts, Bucket=self.bucket, Key=self._key(key), UploadId=upload_id)
        etags = [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in listed.get("Parts", [])]
        if len(etags) != parts:
            raise FileNotFoundError(f"expected {parts} parts, found {len(etags)}")
        await asyncio.to_thread(
            self.s3.complete_multipart_upload,
            Bucket=self.bucket,
            Key=self._key(key),
            UploadId=upload_id,
            MultipartUpload={"Parts": etags},
        )
        head = await asyncio.to_thread(self.s3.head_object, Bucket=self.bucket, Key=self._key(key))
        return int(head["ContentLength"])

    async def download_url(self, key: str, filename: str) -> str:
        return await asyncio.to_thread(
            self.s3.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key), "ResponseContentDisposition": f'attachment; filename="{filename}"'},
            ExpiresIn=URL_TTL_S,
        )

    @contextlib.asynccontextmanager
    async def local_copy(self, key: str) -> AsyncIterator[Path]:
        cache = Path(self.settings.data_dir) / "blob-cache"
        cache.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=cache, suffix=".zip")
        os.close(fd)
        try:
            await asyncio.to_thread(self.s3.download_file, self.bucket, self._key(key), name)
            yield Path(name)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.s3.delete_object, Bucket=self.bucket, Key=self._key(key))


def make_blob_store(settings: Settings) -> BlobStore:
    return S3BlobStore(settings) if settings.blob_backend == "s3" else LocalBlobStore(settings)
