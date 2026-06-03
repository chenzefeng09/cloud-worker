"""Lightweight S3/MinIO client for session attachment blobs.

Pattern mirrors knowledge-ingest/blob/s3.py — kept self-contained so the
orchestrator doesn't need to import knowledge-ingest internals.
"""

from __future__ import annotations

from typing import Any


class S3BlobStore:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import boto3  # type: ignore[import-not-found]

            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
            )
        self._bucket = bucket
        self._client = client

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type,
        )
        return f"s3://{self._bucket}/{key}"

    def get(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        return obj["Body"].read()  # type: ignore[no-any-return]

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
