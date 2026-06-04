"""S3 BlobStore — production backend."""

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
            import boto3

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
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self._bucket}/{key}"

    def get(self, key: str) -> bytes:
        obj = self._client.get_object(Bucket=self._bucket, Key=key)
        data: bytes = obj["Body"].read()
        return data

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def presign_get(self, key: str, *, ttl_seconds: int = 600) -> str:
        """Return a pre-signed URL the client can GET directly."""
        result: str = self._client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        return result

    def list_prefix(self, prefix: str) -> list[str]:
        """Return all object keys under ``prefix`` (handles pagination)."""
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys
