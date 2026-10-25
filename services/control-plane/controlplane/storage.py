"""Blob storage for dataset packages: local filesystem or S3-compatible.

Uploads and downloads go straight between the client and the store via signed
URLs (S3 presigned URLs, or HMAC-signed URLs to this service for the local
backend), matching the multipart flow the upstream dataset skills implement.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from .security import sign, verify_signature
from .settings import Settings

URL_TTL_S = 3600


class BlobStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # parts are staged under "<key>.parts/<n>" then combined
    async def part_upload_url(self, key: str, part_number: int) -> str:
        raise NotImplementedError

    async def complete_parts(self, key: str, parts: int) -> int:
        raise NotImplementedError

    async def download_url(self, key: str, filename: str) -> str:
        raise NotImplementedError

    async def open_local(self, key: str) -> Path:
        """Path to a local copy (downloaded for S3) - used for validation."""
        raise NotImplementedError

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

    async def part_upload_url(self, key: str, part_number: int) -> str:
        payload = f"{key}#{part_number}"
        return self._signed(f"/api/v1/blobs/upload/{quote(key)}", "blob-put", payload, {"part": str(part_number)})

    def verify_part(self, key: str, part_number: int, exp: int, sig: str) -> bool:
        return verify_signature(self.settings, "blob-put", f"{key}#{part_number}", exp, sig)

    def write_part(self, key: str, part_number: int, data: bytes) -> None:
        parts_dir = self.path(key + ".parts")
        parts_dir.mkdir(parents=True, exist_ok=True)
        (parts_dir / f"{part_number:06d}").write_bytes(data)

    async def complete_parts(self, key: str, parts: int) -> int:
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

    async def open_local(self, key: str) -> Path:
        return self.path(key)

    async def delete(self, key: str) -> None:
        with_parts = self.path(key + ".parts")
        shutil.rmtree(with_parts, ignore_errors=True)
        try:
            os.unlink(self.path(key))
        except FileNotFoundError:
            pass


class S3BlobStore(BlobStore):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        import boto3  # optional dependency: controlplane[s3]

        self.bucket = settings.s3_bucket
        self.prefix = settings.s3_prefix
        self.s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint_url or None, region_name=settings.s3_region or None)
        self._uploads: dict[str, str] = {}
        self.cache = (Path(settings.data_dir) / "blob-cache").resolve()
        self.cache.mkdir(parents=True, exist_ok=True)

    def _key(self, key: str) -> str:
        return self.prefix + key

    async def _upload_id(self, key: str) -> str:
        if key not in self._uploads:
            resp = await asyncio.to_thread(self.s3.create_multipart_upload, Bucket=self.bucket, Key=self._key(key))
            self._uploads[key] = resp["UploadId"]
        return self._uploads[key]

    async def part_upload_url(self, key: str, part_number: int) -> str:
        upload_id = await self._upload_id(key)
        return await asyncio.to_thread(
            self.s3.generate_presigned_url,
            "upload_part",
            Params={"Bucket": self.bucket, "Key": self._key(key), "UploadId": upload_id, "PartNumber": part_number},
            ExpiresIn=URL_TTL_S,
        )

    async def complete_parts(self, key: str, parts: int) -> int:
        upload_id = await self._upload_id(key)
        listed = await asyncio.to_thread(self.s3.list_parts, Bucket=self.bucket, Key=self._key(key), UploadId=upload_id)
        etags = [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in listed.get("Parts", [])]
        if len(etags) != parts:
            raise FileNotFoundError(f"expected {parts} parts, found {len(etags)}")
        await asyncio.to_thread(
            self.s3.complete_multipart_upload,
            Bucket=self.bucket, Key=self._key(key), UploadId=upload_id, MultipartUpload={"Parts": etags},
        )
        self._uploads.pop(key, None)
        head = await asyncio.to_thread(self.s3.head_object, Bucket=self.bucket, Key=self._key(key))
        return int(head["ContentLength"])

    async def download_url(self, key: str, filename: str) -> str:
        return await asyncio.to_thread(
            self.s3.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key), "ResponseContentDisposition": f'attachment; filename="{filename}"'},
            ExpiresIn=URL_TTL_S,
        )

    async def open_local(self, key: str) -> Path:
        target = self.cache / key.replace("/", "_")
        await asyncio.to_thread(self.s3.download_file, self.bucket, self._key(key), str(target))
        return target

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.s3.delete_object, Bucket=self.bucket, Key=self._key(key))


def make_blob_store(settings: Settings) -> BlobStore:
    return S3BlobStore(settings) if settings.blob_backend == "s3" else LocalBlobStore(settings)
