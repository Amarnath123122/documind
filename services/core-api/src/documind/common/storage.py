"""Blob storage adapter (Azure Blob / Azurite).

Containers: `quarantine` (untrusted uploads, never served) and `originals` (scanned bytes).
SAS URLs are bearer capabilities: never log them.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from documind.common.config import get_settings

QUARANTINE = "quarantine"
ORIGINALS = "originals"


@dataclass(frozen=True)
class BlobInfo:
    size: int
    etag: str


class BlobStorage:
    """Two modes, chosen by the connection setting:

    - "DefaultEndpointsProtocol=...;AccountKey=..." (Azurite/local): key-signed service SAS.
    - "https://<account>.blob.core.windows.net" (production): managed identity, no keys at
      all; SAS is a *user-delegation* SAS. NOT exercised by automated tests: Azurite cannot
      emulate it. Verify against a real account before launch (ADR-0009).
    """

    def __init__(self, connection_string: str, public_base: str):
        self._conn = connection_string
        self._public_base = public_base.rstrip("/")
        self._identity = connection_string.startswith("https://")
        if self._identity:
            self._account = connection_string.split("//", 1)[1].split(".", 1)[0]
            self._key = None
        else:
            parts = dict(p.split("=", 1) for p in connection_string.split(";") if "=" in p)
            self._account, self._key = parts["AccountName"], parts["AccountKey"]

    def _client(self) -> BlobServiceClient:
        if self._identity:
            from azure.identity.aio import DefaultAzureCredential
            return BlobServiceClient(self._conn, credential=DefaultAzureCredential())
        return BlobServiceClient.from_connection_string(self._conn)

    async def upload_url(self, container: str, path: str, ttl_s: int) -> str:
        """Write-only, single-object, short-lived. Cannot read, list or delete."""
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=ttl_s)
        signing: dict = {"account_key": self._key}
        if self._identity:
            async with self._client() as c:
                signing = {"user_delegation_key": await c.get_user_delegation_key(
                    key_start_time=now - timedelta(minutes=5), key_expiry_time=expiry)}
        sas = generate_blob_sas(
            account_name=self._account, container_name=container, blob_name=path,
            permission=BlobSasPermissions(create=True, write=True), expiry=expiry, **signing)
        return f"{self._public_base}/{container}/{path}?{sas}"

    async def info(self, container: str, path: str) -> BlobInfo | None:
        async with self._client() as c:
            try:
                p = await c.get_blob_client(container, path).get_blob_properties()
            except ResourceNotFoundError:
                return None
        return BlobInfo(size=p.size, etag=p.etag)

    async def read_head(self, container: str, path: str, n: int) -> bytes:
        async with self._client() as c:
            stream = await c.get_blob_client(container, path).download_blob(offset=0, length=n)
            return await stream.readall()

    async def read_all(self, container: str, path: str, *, etag: str | None = None) -> bytes:
        """With etag, fails if the blob changed since it was verified."""
        async with self._client() as c:
            kw = {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag else {}
            stream = await c.get_blob_client(container, path).download_blob(**kw)
            return await stream.readall()

    async def write(self, container: str, path: str, data: bytes) -> None:
        async with self._client() as c:
            await c.get_blob_client(container, path).upload_blob(data, overwrite=True)

    async def delete(self, container: str, path: str) -> None:
        async with self._client() as c:
            try:
                await c.get_blob_client(container, path).delete_blob()
            except ResourceNotFoundError:
                pass

    async def ensure_containers(self) -> None:
        async with self._client() as c:
            for name in (QUARANTINE, ORIGINALS):
                try:
                    await c.create_container(name)
                except ResourceExistsError:
                    pass


def get_storage() -> BlobStorage:
    s = get_settings()
    return BlobStorage(s.blob_connection_string, s.blob_public_base)
