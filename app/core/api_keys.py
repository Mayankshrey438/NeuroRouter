"""
API key management, backed by Redis so keys can be created/revoked at
runtime through the admin API/UI instead of only via a static env var.

Each key is stored as a Redis hash at `apikey:{key}`:
    name, created_at, revoked ("0"/"1"), last_used_at, request_count

`apikeys:index` is a Redis SET of all key strings, so listing doesn't
require a KEYS/SCAN over the whole keyspace.

On first startup, any keys from NEUROROUTER_API_KEYS (.env) are seeded in
as a "Default Key" / "Seed Key N" entry if the store is empty, so the
out-of-the-box dev-key-123 keeps working AND shows up in the UI.
"""
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from redis.asyncio import Redis

INDEX_KEY = "apikeys:index"


def _key_hash(key: str) -> str:
    return f"apikey:{key}"


def generate_key() -> str:
    return f"nr_live_{uuid.uuid4().hex}"


@dataclass
class ApiKeyRecord:
    key: str
    name: str
    created_at: float
    revoked: bool
    last_used_at: Optional[float]
    request_count: int

    def public_dict(self, reveal_full: bool = False) -> dict:
        masked = self.key if reveal_full else f"{self.key[:11]}…{self.key[-4:]}"
        return {
            "key": masked,
            "name": self.name,
            "created_at": self.created_at,
            "revoked": self.revoked,
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
        }


class ApiKeyStore:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def seed_if_empty(self, seed_keys: List[str]):
        count = await self.redis.scard(INDEX_KEY)
        if count > 0 or not seed_keys:
            return
        for i, key in enumerate(seed_keys):
            name = "Default Key" if i == 0 else f"Seed Key {i + 1}"
            await self._create_raw(key, name)

    async def _create_raw(self, key: str, name: str) -> ApiKeyRecord:
        record = {
            "name": name,
            "created_at": str(time.time()),
            "revoked": "0",
            "last_used_at": "",
            "request_count": "0",
        }
        pipe = self.redis.pipeline()
        pipe.hset(_key_hash(key), mapping=record)
        pipe.sadd(INDEX_KEY, key)
        await pipe.execute()
        return ApiKeyRecord(key=key, name=name, created_at=time.time(), revoked=False,
                             last_used_at=None, request_count=0)

    async def create(self, name: str) -> ApiKeyRecord:
        key = generate_key()
        return await self._create_raw(key, name or "Unnamed Key")

    async def list(self) -> List[ApiKeyRecord]:
        keys = await self.redis.smembers(INDEX_KEY)
        records = []
        for key in keys:
            data = await self.redis.hgetall(_key_hash(key))
            if not data:
                continue
            records.append(ApiKeyRecord(
                key=key,
                name=data.get("name", "Unnamed"),
                created_at=float(data.get("created_at", 0)),
                revoked=data.get("revoked") == "1",
                last_used_at=float(data["last_used_at"]) if data.get("last_used_at") else None,
                request_count=int(data.get("request_count", 0)),
            ))
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    async def revoke(self, key: str) -> bool:
        exists = await self.redis.exists(_key_hash(key))
        if not exists:
            return False
        await self.redis.hset(_key_hash(key), "revoked", "1")
        return True

    async def delete(self, key: str) -> bool:
        pipe = self.redis.pipeline()
        pipe.delete(_key_hash(key))
        pipe.srem(INDEX_KEY, key)
        results = await pipe.execute()
        return bool(results[0])

    async def validate(self, key: Optional[str]) -> Optional[ApiKeyRecord]:
        """Returns the ApiKeyRecord if the key is valid & not revoked, else None."""
        if not key:
            return None
        data = await self.redis.hgetall(_key_hash(key))
        if not data or data.get("revoked") == "1":
            return None
        return ApiKeyRecord(
            key=key,
            name=data.get("name", "Unnamed"),
            created_at=float(data.get("created_at", 0)),
            revoked=False,
            last_used_at=float(data["last_used_at"]) if data.get("last_used_at") else None,
            request_count=int(data.get("request_count", 0)),
        )

    async def touch(self, key: str):
        pipe = self.redis.pipeline()
        pipe.hset(_key_hash(key), "last_used_at", str(time.time()))
        pipe.hincrby(_key_hash(key), "request_count", 1)
        await pipe.execute()
