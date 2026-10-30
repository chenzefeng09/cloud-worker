"""Erasure ledger — the record of GDPR erasures that a backup restore can't undo.

Restoring a backup (pgBackRest PITR or full restore, ClickHouse, a Velero
snapshot) brings back the rows of every user erased after the backup was
taken.  Erasing them again needs a list of those erasures that the restore did
not rewind, so the list cannot live next to the data:

* the BFF tables sit in the same ``foundation`` database pgBackRest restores;
* foundation-audit queues events in that database (``audit.outbox``) and
  copies them to ClickHouse, which a full recovery restores from its daily
  backup or rebuilds from the rewound outbox.  Its query API also answers one
  tenant per call, privacy events (severity NOTICE) never reach its WORM
  bucket, and the BFF emits them best-effort.

A record stored with the data shares the data's recovery point, so a restore
loses exactly the erasures that need re-applying.  The ledger is therefore one
small JSON object per erasure request in a dedicated S3 bucket with Object
Lock: COMPLIANCE mode stops anyone overwriting or deleting an entry for
``privacy_ledger_retention_days``, and no restore procedure rewinds object
storage (the bucket is mirrored like the audit WORM bucket).

An entry holds identifiers and a timestamp only — tenant, user, who asked and
through which route — never erased content.  Keys start with the request time
in UTC, so "every erasure since the backup" is a single listing.

The erasure routes write the entry before the first step runs and refuse the
request when the ledger is configured but unreachable (``privacy.py``);
``agent_bff.erasure_replay`` reads the entries back after a restore.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_bff.config import Settings

_PREFIX = "erasures/"
# Fixed width and UTC, so keys sort in request-time order.
_KEY_TIME = "%Y%m%dT%H%M%S.%fZ"
_ENTRY_VERSION = 1
_ENTRY_FIELDS = ("entry_id", "tenant_id", "user_id", "via", "actor_user_id")


class LedgerError(Exception):
    """The ledger could not be written, listed or read."""


def _key_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_KEY_TIME)


@dataclass(frozen=True)
class LedgerEntry:
    """One erasure request, as recorded."""

    entry_id: str
    requested_at: datetime
    tenant_id: str
    user_id: str
    via: str
    actor_user_id: str

    @property
    def key(self) -> str:
        return f"{_PREFIX}{_key_time(self.requested_at)}-{self.entry_id}.json"

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "version": _ENTRY_VERSION,
                "entry_id": self.entry_id,
                "requested_at": self.requested_at.isoformat(),
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "via": self.via,
                "actor_user_id": self.actor_user_id,
            },
            sort_keys=True,
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> LedgerEntry:
        """Parse a stored entry; ``ValueError`` when it is not one."""
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("ledger entry is not a JSON object")
        values: dict[str, str] = {}
        for name in _ENTRY_FIELDS:
            value = body.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ledger entry has no {name}")
            values[name] = value
        requested_at = datetime.fromisoformat(str(body.get("requested_at")))
        if requested_at.tzinfo is None:
            raise ValueError("ledger entry time has no time zone")
        return cls(requested_at=requested_at, **values)


def _s3_client(
    *, region: str, endpoint_url: str, access_key_id: str, secret_access_key: str,
) -> Any:
    """A boto3 S3 client on its own session (safe to build in a worker thread)."""
    import boto3
    from botocore.config import Config

    return boto3.session.Session().client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url or None,
        aws_access_key_id=access_key_id or None,
        aws_secret_access_key=secret_access_key or None,
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 3}),
    )


class ErasureLedger:
    """Append-only erasure entries in an Object Lock bucket (blocking S3 calls
    run in a worker thread)."""

    def __init__(
        self,
        *,
        bucket: str,
        retention_days: int,
        region: str = "us-east-1",
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
    ) -> None:
        self._bucket = bucket
        self._retention = timedelta(days=retention_days)
        self._client_options = {
            "region": region,
            "endpoint_url": endpoint_url,
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
        }
        self._client: Any = None

    @classmethod
    def from_settings(cls, settings: Settings) -> ErasureLedger | None:
        """The configured ledger, or ``None`` when no bucket is set."""
        if not settings.privacy_ledger_s3_bucket:
            return None
        return cls(
            bucket=settings.privacy_ledger_s3_bucket,
            retention_days=settings.privacy_ledger_retention_days,
            region=settings.privacy_ledger_s3_region,
            endpoint_url=settings.privacy_ledger_s3_endpoint_url,
            access_key_id=settings.privacy_ledger_s3_access_key_id,
            secret_access_key=settings.privacy_ledger_s3_secret_access_key,
        )

    def _s3(self) -> Any:
        if self._client is None:
            self._client = _s3_client(**self._client_options)
        return self._client

    async def record(
        self, *, tenant_id: str, user_id: str, via: str, actor_user_id: str,
    ) -> LedgerEntry:
        """Store one entry, locked for the retention period.

        Raises ``LedgerError`` when the entry was not stored.
        """
        entry = LedgerEntry(
            entry_id=uuid.uuid4().hex,
            requested_at=datetime.now(UTC),
            tenant_id=tenant_id,
            user_id=user_id,
            via=via,
            actor_user_id=actor_user_id,
        )
        try:
            await asyncio.to_thread(self._put, entry)
        except Exception as exc:  # noqa: BLE001 — any client or network failure
            raise LedgerError(f"could not record the erasure ({type(exc).__name__})") from exc
        return entry

    def _put(self, entry: LedgerEntry) -> None:
        body = entry.to_json()
        self._s3().put_object(
            Bucket=self._bucket,
            Key=entry.key,
            Body=body,
            ContentType="application/json",
            # S3 rejects an Object Lock write without an integrity checksum.
            ContentMD5=base64.b64encode(
                hashlib.md5(body, usedforsecurity=False).digest()
            ).decode(),
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=entry.requested_at + self._retention,
        )

    async def entries_since(self, since: datetime) -> list[LedgerEntry]:
        """Every entry requested at or after ``since`` (timezone-aware), oldest
        first.  Raises ``LedgerError`` when the ledger can't be listed or an
        entry can't be read — a replay must not silently skip anyone."""
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        try:
            return await asyncio.to_thread(self._entries_since, since)
        except LedgerError:
            raise
        except Exception as exc:  # noqa: BLE001 — any client or network failure
            raise LedgerError(f"could not read the ledger ({type(exc).__name__})") from exc

    def _entries_since(self, since: datetime) -> list[LedgerEntry]:
        # Keys are "<prefix><time>-<id>.json", so every entry at or after
        # ``since`` sorts after "<prefix><since>"; S3 lists keys in order.
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": _PREFIX,
            "StartAfter": f"{_PREFIX}{_key_time(since)}",
        }
        entries: list[LedgerEntry] = []
        while True:
            page = self._s3().list_objects_v2(**request)
            for item in page.get("Contents", []):
                entries.append(self._read(str(item["Key"])))
            if not page.get("IsTruncated"):
                return entries
            request["ContinuationToken"] = page["NextContinuationToken"]

    def _read(self, key: str) -> LedgerEntry:
        raw = self._s3().get_object(Bucket=self._bucket, Key=key)["Body"].read()
        try:
            return LedgerEntry.from_json(raw)
        except ValueError as exc:
            raise LedgerError(f"unreadable ledger entry {key}") from exc
