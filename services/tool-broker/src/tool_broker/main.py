"""FastAPI app for sandbox tool-broker façades."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from tool_broker.config import Settings

_LOG = logging.getLogger(__name__)

# Redis key schema shared with the worker's RunStateClient.  Keys are ALWAYS
# derived from a run_id taken from the validated lease, never from a value the
# sandbox can choose freely.
_STATE_KEY_KINDS = frozenset({"state", "paused"})


def _state_key(run_id: str, kind: str) -> str:
    """Map a (run_id, kind) pair to the canonical hot-state Redis key.

    ``kind`` ∈ {``state``, ``paused``} maps to ``agent:run:{run_id}:state`` /
    ``agent:run:{run_id}:paused`` — matching ``redis_state.RunStateClient``.
    """
    return f"agent:run:{run_id}:{kind}"


def _token_stream_key(run_id: str) -> str:
    """Map a run_id to the token-stream key ``agent:run:{run_id}:tokens``."""
    return f"agent:run:{run_id}:tokens"

_HOP_BY_HOP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-forwarded-for",
        "x-real-ip",
        "x-tool-broker-lease",
        # Sandbox-supplied identity/authz headers are NEVER trusted: a sandbox
        # could set ``X-Actor: ADMIN`` (an unconditional RBAC bypass on the
        # mcp-registry / skill-store internal guards) or spoof another tenant /
        # user.  We strip them here and re-inject ONLY the identity proven by
        # the validated lease (see ``_forward_headers``).
        "x-actor",
        "x-service-scopes",
        "x-user-id",
        "x-tenant-id",
    }
)

_HOP_BY_HOP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

_ALLOWED_METHODS = frozenset({"GET", "POST", "HEAD"})


# ---------------------------------------------------------------------------
# B2: per-route upstream path allowlist
# ---------------------------------------------------------------------------
#
# Each passthrough proxy route forwards a sandbox-chosen ``{path}`` to its
# upstream service.  Left unconstrained, ``{path}`` is an open door into the
# upstream's whole GET/POST surface.  We pin each route to the SMALL set of
# upstream paths the worker's in-sandbox extensions actually call, derived by
# reading those extensions (file:line cited per entry).  The path is normalised
# (leading/trailing slashes stripped, query already dropped, ``..`` traversal
# rejected) and compared case-sensitively.  ``exact`` entries must match the
# whole normalised path; ``prefixes`` entries match the path or any deeper
# child (``entry`` or ``entry/...``).
#
# Correctness (never break a legit worker call) beats tightness: where the live
# call set cannot be confirmed (skills — see below) we use a narrow ``/internal``
# prefix rather than an over-tight exact rule.


class _UpstreamAllowlist:
    """An allowlist of permitted upstream paths for one proxy route.

    ``exact`` paths must equal the normalised request path; ``prefixes`` match
    the path itself or any deeper segment (``p`` or ``p/<rest>``).  All entries
    are stored without leading/trailing slashes and compared case-sensitively.
    """

    def __init__(
        self,
        *,
        exact: frozenset[str] = frozenset(),
        prefixes: tuple[str, ...] = (),
    ) -> None:
        self.exact = frozenset(p.strip("/") for p in exact)
        self.prefixes = tuple(p.strip("/") for p in prefixes)

    def allows(self, normalized_path: str) -> bool:
        """Return ``True`` when ``normalized_path`` is permitted by this route."""
        if normalized_path in self.exact:
            return True
        return any(
            normalized_path == prefix or normalized_path.startswith(prefix + "/")
            for prefix in self.prefixes
        )


# The route key is the required scope (already threaded through ``_proxy``),
# which uniquely identifies each passthrough route.
_UPSTREAM_ALLOWLISTS: dict[str, _UpstreamAllowlist] = {
    # llm-gateway: the OpenAI SDK in llm_gateway_enforcer.model._build_openai_
    # gateway_model only ever POSTs chat completions — the SDK appends
    # ``/chat/completions`` to ``LLM_GATEWAY_URL`` (model.py docstring lines
    # 5-8 and 122-124).  The enforcer never calls ``/models`` or any other
    # endpoint, so the tightest correct rule is the single exact path.
    "llm-gateway.invoke": _UpstreamAllowlist(
        exact=frozenset({"chat/completions"}),
    ),
    # mcp-registry: mcp_tenant_injector.injector.fetch_mcp_servers issues a
    # single GET to ``{base}/internal/mcp-servers`` (injector.py:56).  Exact.
    "mcp.read_internal": _UpstreamAllowlist(
        exact=frozenset({"internal/mcp-servers"}),
    ),
    # skill-store: the in-sandbox skills_loader no longer resolves over the
    # network (skills are bind-mounted read-only — skills_loader/__init__.py
    # module docstring; egress_policy.py:141-143 keeps SKILL_STORE_URL only
    # for legacy sandbox-side loaders).  So there is NO confirmed live call on
    # this route today.  The legacy loader hit skill-store's internal endpoints
    # (skill_store/api/v1/skills.py:260 ``/internal/skills`` and :299
    # ``/internal/skills/resolve``).  We therefore allow the ``internal/skills``
    # PREFIX — meaningfully narrower than the open passthrough, and it keeps any
    # legacy loader working — rather than guessing a too-tight exact rule.
    "skill.read_internal": _UpstreamAllowlist(
        prefixes=("internal/skills",),
    ),
    # web-fetch-gateway: web_fetch.tool.WebFetchTool._fetch_text POSTs a single
    # ``/fetch`` endpoint (tool.py:200-203; web_fetch/__init__.py docstring
    # line 15).  Exact.
    "web_fetch.invoke": _UpstreamAllowlist(
        exact=frozenset({"fetch"}),
    ),
}


def _normalize_upstream_path(path: str) -> str:
    """Normalise a proxy ``{path}`` for allowlist comparison.

    Strips leading/trailing slashes (the query string is already excluded by
    FastAPI's ``{path:path}`` capture) and rejects path traversal: any segment
    equal to ``.`` or ``..`` raises 403 so a sandbox cannot climb out of the
    intended upstream surface.
    """
    candidate = (path or "").strip("/")
    segments = candidate.split("/") if candidate else []
    if any(segment in ("", ".", "..") for segment in segments):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker: upstream path not allowed",
        )
    return candidate


def _enforce_upstream_allowlist(
    settings: Settings, *, required_scope: str, path: str
) -> None:
    """Reject ``path`` unless it is on ``required_scope``'s route allowlist.

    No-op when ``enforce_upstream_allowlist`` is disabled (dev/tests).  Raises
    403 ``tool-broker: upstream path not allowed`` for an unknown route or a
    path outside the route's allowlist.
    """
    if not settings.enforce_upstream_allowlist:
        return
    normalized = _normalize_upstream_path(path)
    allowlist = _UPSTREAM_ALLOWLISTS.get(required_scope)
    if allowlist is None or not allowlist.allows(normalized):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker: upstream path not allowed",
        )


router = APIRouter(tags=["Tool Broker"])


def _clean_base(value: str) -> str:
    return value.rstrip("/")


def _target_url(base_url: str, path: str, query: str) -> str:
    url = f"{_clean_base(base_url)}/{path.lstrip('/')}" if path else _clean_base(base_url)
    if query:
        url = f"{url}?{query}"
    return url


def _require_auth(request: Request, settings: Settings) -> str:
    # TODO(security, B7): the Bearer token here is a real JWT minted by the
    # identity service (audience "foundation", tenant-bound), but the broker
    # only does a prefix/presence check below — it does NOT verify the
    # signature.  Full verification was deferred because the broker has no
    # access to the identity service's signing key: there is no JWKS URL or
    # public-key config field (see config.Settings), no JWT library in
    # pyproject.toml (fastapi/httpx/redis/boto3 only), and no foundation_sdk
    # available to it; the JWKS lives in the separate saas-foundation repo.
    # Bolting on PyJWT + cross-repo key fetching would add a fragile new
    # dependency for marginal gain — the HMAC per-run lease (_require_lease,
    # signed by the scheduler's tool_broker_lease_secret) is already the real
    # per-run authority enforced on every façade.  To implement later: add a
    # ``identity_jwks_url`` / ``identity_public_key`` config field, and when it
    # is set verify the token's signature + ``aud == "foundation"`` + ``exp`` +
    # that the token's tenant claim matches ``X-Tenant-Id`` here; when it is
    # unset, keep the current prefix-only behaviour so dev/tests don't break.
    if not settings.require_auth:
        return request.headers.get("X-Tenant-Id", "")

    auth = request.headers.get("Authorization", "")
    tenant_id = request.headers.get("X-Tenant-Id", "")
    if not auth.startswith("Bearer ") or not auth.removeprefix("Bearer ").strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization bearer token required",
        )
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required",
        )
    return tenant_id


def _forward_headers(
    request: Request,
    lease_payload: dict | None,
    *,
    inject_service_scope: str | None = None,
) -> dict[str, str]:
    """Build the upstream header set: denylist filter + trusted re-injection.

    The denylist (``_HOP_BY_HOP_REQUEST_HEADERS``) drops the four
    sandbox-supplied identity/authz headers (``X-Actor``, ``X-Service-Scopes``,
    ``X-User-Id``, ``X-Tenant-Id``) so a sandbox can never inject them.  When a
    validated ``lease_payload`` is present we re-inject ONLY the identity proven
    by the lease — never ``X-Actor`` (the broker is not an admin caller).  When
    enforcement is disabled (``lease_payload is None``, dev only) the headers
    were still stripped by the denylist, which is the safe default.

    ``inject_service_scope`` is the trusted ``X-Service-Scopes`` value the
    broker re-injects for upstreams whose internal guards consume it
    (mcp-registry / skill-store / web-fetch-gateway).  It is derived from the
    broker route — NOT from the (now-stripped) request header — and is only
    set after ``_require_lease`` has proven the lease carries the corresponding
    façade scope, so injecting the fixed upstream scope is safe.  Upstreams that
    do not consume the header (llm-gateway) pass ``None`` and get nothing.
    """
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_REQUEST_HEADERS
    }
    if lease_payload is not None:
        headers["X-Tenant-Id"] = str(lease_payload.get("tenant_id") or "")
        if lease_payload.get("user_id"):
            headers["X-User-Id"] = str(lease_payload["user_id"])
    if inject_service_scope:
        headers["X-Service-Scopes"] = inject_service_scope
    return headers


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _parse_lease(token: str, secret: str) -> dict:
    try:
        version, body, signature = token.split(".", 2)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease malformed",
        ) from exc
    if version != "v1":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease version unsupported",
        )

    expected = base64.urlsafe_b64encode(
        hmac.new(
            secret.encode("utf-8"),
            body.encode("ascii"),
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease signature invalid",
        )

    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease payload invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease payload invalid",
        )
    return payload


def _require_lease(
    request: Request,
    settings: Settings,
    *,
    required_scope: str,
    tenant_id: str,
) -> dict | None:
    """Validate the per-run broker lease and return its decoded payload.

    Returns ``None`` when lease enforcement is disabled (so handlers that need
    the ``run_id`` must guard for that case).  On any validation failure an
    ``HTTPException`` is raised — the proxy/façade handler never proceeds.
    """
    if not settings.require_lease:
        return None
    if not settings.lease_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="tool-broker lease_secret is not configured",
        )

    token = request.headers.get("X-Tool-Broker-Lease", "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-Tool-Broker-Lease header required",
        )
    payload = _parse_lease(token, settings.lease_secret)

    now = int(time.time())
    skew = max(int(settings.lease_clock_skew_seconds), 0)
    try:
        exp = int(payload.get("exp"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease exp invalid",
        ) from exc
    if exp + skew < now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease expired",
        )

    lease_tenant = str(payload.get("tenant_id") or "")
    if not lease_tenant or lease_tenant != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease tenant mismatch",
        )

    request_user = request.headers.get("X-User-Id", "").strip()
    lease_user = str(payload.get("user_id") or "")
    if request_user and lease_user and request_user != lease_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease user mismatch",
        )

    scopes = {
        str(scope).strip()
        for scope in payload.get("scopes", [])
        if str(scope).strip()
    }
    if required_scope not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"tool-broker lease missing scope {required_scope!r}",
        )

    return payload


async def _proxy(
    request: Request,
    *,
    upstream_base_url: str,
    path: str,
    required_scope: str,
    upstream_service_scope: str | None = None,
) -> Response:
    settings: Settings = request.app.state.settings
    tenant_id = _require_auth(request, settings)
    lease_payload = _require_lease(
        request,
        settings,
        required_scope=required_scope,
        tenant_id=tenant_id,
    )

    if request.method.upper() not in _ALLOWED_METHODS:
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail=f"method {request.method!r} is not allowed through tool-broker",
        )

    # B2: constrain the open ``{path}`` passthrough to the route's allowlist of
    # known upstream paths before we build or forward the upstream request.
    _enforce_upstream_allowlist(settings, required_scope=required_scope, path=path)

    query = urlencode(list(request.query_params.multi_items()))
    target = _target_url(upstream_base_url, path, query)
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=settings.proxy_timeout_seconds) as client:
            upstream = await client.request(
                request.method,
                target,
                headers=_forward_headers(
                    request,
                    lease_payload,
                    inject_service_scope=upstream_service_scope,
                ),
                content=body,
            )
    except httpx.HTTPError as exc:
        _LOG.warning("tool-broker upstream error for %s: %s", target, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tool-broker upstream unavailable",
        ) from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


# ---------------------------------------------------------------------------
# /state façade (Redis hot-state)
# ---------------------------------------------------------------------------


def _get_redis(request: Request):
    """Return the broker's own Redis client, building it lazily on first use.

    The client is cached on ``app.state.redis_client`` so a single async
    connection pool is reused across requests.  Tests may pre-populate
    ``app.state.redis_client`` with a fake to avoid a live Redis / the
    ``redis`` package.  ``redis.asyncio`` is imported lazily so the module
    imports cleanly in environments without the package.
    """
    client = getattr(request.app.state, "redis_client", None)
    if client is not None:
        return client
    settings: Settings = request.app.state.settings
    try:
        import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        _LOG.error("tool-broker redis client unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tool-broker state backend unavailable",
        ) from exc
    client = redis_asyncio.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=2,
        socket_timeout=settings.proxy_timeout_seconds,
        decode_responses=True,
    )
    request.app.state.redis_client = client
    return client


def _lease_run_id(payload: dict | None) -> str:
    """Extract the run_id from a validated lease payload (or ``""``)."""
    if not payload:
        return ""
    return str(payload.get("run_id") or "")


def _require_state_run_id(request_run_id: str, lease_payload: dict | None) -> str:
    """Enforce the isolation boundary: request run_id must match the lease.

    When the lease is enforced, the ``run_id`` supplied by the sandbox MUST
    equal the ``run_id`` baked into its lease — otherwise a sandbox could read
    or mutate another run's keys.  When lease enforcement is disabled (dev),
    the request run_id is trusted as-is.
    """
    request_run_id = (request_run_id or "").strip()
    if not request_run_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id is required",
        )
    lease_run_id = _lease_run_id(lease_payload)
    if lease_run_id and request_run_id != lease_run_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker lease run_id mismatch",
        )
    return request_run_id


class _StateSetBody(BaseModel):
    run_id: str
    kind: str
    value: str
    ttl: int = Field(gt=0)


class _StateXAddBody(BaseModel):
    run_id: str
    fields: dict[str, str]
    maxlen: int = Field(default=10_000, gt=0)
    expire: int | None = None


@router.post("/state/v1/set")
async def state_set(body: _StateSetBody, request: Request) -> dict[str, bool]:
    """``SET agent:run:{run_id}:{state|paused} value EX ttl`` via the broker."""
    settings: Settings = request.app.state.settings
    tenant_id = _require_auth(request, settings)
    lease = _require_lease(
        request, settings, required_scope="state.write", tenant_id=tenant_id
    )
    run_id = _require_state_run_id(body.run_id, lease)
    if body.kind not in _STATE_KEY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported state kind {body.kind!r}",
        )

    client = _get_redis(request)
    try:
        await client.set(_state_key(run_id, body.kind), body.value, ex=body.ttl)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("tool-broker state set failed for run %s: %s", run_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tool-broker state backend error",
        ) from exc
    return {"ok": True}


@router.get("/state/v1/get")
async def state_get(request: Request) -> dict[str, str | None]:
    """``GET agent:run:{run_id}:{state|paused}`` via the broker."""
    settings: Settings = request.app.state.settings
    tenant_id = _require_auth(request, settings)
    lease = _require_lease(
        request, settings, required_scope="state.write", tenant_id=tenant_id
    )
    run_id = _require_state_run_id(request.query_params.get("run_id", ""), lease)
    kind = request.query_params.get("kind", "state")
    if kind not in _STATE_KEY_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported state kind {kind!r}",
        )

    client = _get_redis(request)
    try:
        value = await client.get(_state_key(run_id, kind))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("tool-broker state get failed for run %s: %s", run_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tool-broker state backend error",
        ) from exc
    return {"value": value}


@router.post("/state/v1/xadd")
async def state_xadd(body: _StateXAddBody, request: Request) -> dict[str, bool]:
    """``XADD agent:run:{run_id}:tokens fields MAXLEN ~ maxlen`` via the broker.

    When ``expire`` is supplied the broker also issues
    ``EXPIRE agent:run:{run_id}:tokens expire`` — the worker decides when to
    send it (only on the first push), so the broker simply honours it.
    """
    settings: Settings = request.app.state.settings
    tenant_id = _require_auth(request, settings)
    lease = _require_lease(
        request, settings, required_scope="state.write", tenant_id=tenant_id
    )
    run_id = _require_state_run_id(body.run_id, lease)
    if not body.fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="fields must be a non-empty mapping",
        )

    key = _token_stream_key(run_id)
    client = _get_redis(request)
    try:
        await client.xadd(key, body.fields, maxlen=body.maxlen, approximate=True)
        if body.expire is not None and body.expire > 0:
            await client.expire(key, body.expire)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("tool-broker state xadd failed for run %s: %s", run_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tool-broker state backend error",
        ) from exc
    return {"ok": True}


# ---------------------------------------------------------------------------
# /s3 façade (read-only object store)
# ---------------------------------------------------------------------------


def _get_s3(request: Request):
    """Return the broker's own boto3 S3 client, building it lazily on first use.

    Cached on ``app.state.s3_client``.  Tests may pre-populate it with a fake
    so they need neither boto3 nor a live object store.
    """
    client = getattr(request.app.state, "s3_client", None)
    if client is not None:
        return client
    settings: Settings = request.app.state.settings
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        _LOG.error("tool-broker boto3 unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tool-broker object-store backend unavailable",
        ) from exc
    client = boto3.client(
        "s3",
        region_name=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url or None,
        aws_access_key_id=settings.s3_access_key_id or None,
        aws_secret_access_key=settings.s3_secret_access_key or None,
    )
    request.app.state.s3_client = client
    return client


def _require_s3_key(key: str, lease_payload: dict | None, tenant_id: str) -> str:
    """Enforce that ``key`` lives inside the lease tenant's namespace.

    All object keys the worker reads (prompt files and user attachments) are
    tenant-prefixed ``tenant/{tenant_id}/...`` — see the object-store key
    schema in session-orchestrator (attachments) and the scheduler (prompts /
    artifacts).  So the rule is a strict ``tenant/{tenant_id}/`` prefix match
    against the tenant proven by the lease (falling back to the request
    ``X-Tenant-Id`` only when lease enforcement is disabled).
    """
    key = (key or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key is required",
        )
    lease_tenant = ""
    if lease_payload:
        lease_tenant = str(lease_payload.get("tenant_id") or "")
    scope_tenant = lease_tenant or tenant_id
    if not scope_tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker s3 tenant unknown",
        )
    if key != f"tenant/{scope_tenant}" and not key.startswith(
        f"tenant/{scope_tenant}/"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tool-broker s3 key outside tenant namespace",
        )
    return key


@router.get("/s3/v1/object")
async def s3_object(request: Request) -> Response:
    """``GET object(bucket, key)`` via the broker — read-only, tenant-scoped.

    Returns the raw object bytes (``application/octet-stream``) so the worker's
    failure-open S3 shim can ``.read()`` them.  A missing object yields 404 so
    the worker treats it exactly like ``NoSuchKey``.
    """
    settings: Settings = request.app.state.settings
    tenant_id = _require_auth(request, settings)
    lease = _require_lease(
        request, settings, required_scope="s3.read", tenant_id=tenant_id
    )
    # SECURITY (B8): always use the broker's own configured bucket and ignore
    # any sandbox-supplied ``?bucket=`` param. The broker holds MinIO root
    # credentials, so honouring an attacker-chosen bucket would let a sandbox
    # read any same-key-convention bucket (backups, billing, logs) cross-bucket.
    bucket = settings.s3_bucket.strip()
    key = _require_s3_key(request.query_params.get("key", ""), lease, tenant_id)

    client = _get_s3(request)
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
    except Exception as exc:  # noqa: BLE001
        # Missing object (NoSuchKey) and any other error map to 404 so the
        # worker's failure-open read path returns None rather than crashing.
        _LOG.debug("tool-broker s3 read miss for %s/%s: %s", bucket, key, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="object not found",
        ) from exc
    return Response(content=data, media_type="application/octet-stream")


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.api_route("/llm-gateway/v1/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy_llm_gateway(path: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    return await _proxy(
        request,
        upstream_base_url=settings.llm_gateway_url,
        path=path,
        required_scope="llm-gateway.invoke",
        # llm-gateway authenticates via X-LLM-Virtual-Key and does NOT consume
        # X-Service-Scopes, so the broker injects nothing for this route.
        upstream_service_scope=None,
    )


@router.api_route("/mcp/v1/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy_mcp_registry(path: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    return await _proxy(
        request,
        upstream_base_url=settings.mcp_registry_url,
        path=path,
        required_scope="mcp.read_internal",
        # mcp-registry's require_internal_scope guard 403s unless
        # X-Service-Scopes contains mcp.read_internal; the broker injects the
        # trusted value (the sandbox-supplied header was stripped).
        upstream_service_scope="mcp.read_internal",
    )


@router.api_route("/skills/v1/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy_skill_store(path: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    return await _proxy(
        request,
        upstream_base_url=settings.skill_store_url,
        path=path,
        required_scope="skill.read_internal",
        # skill-store's internal guard 403s unless X-Service-Scopes contains
        # skill.read_internal; the broker injects the trusted value.
        upstream_service_scope="skill.read_internal",
    )


@router.api_route(
    "/web-fetch/api/web-fetch/v1/{path:path}",
    methods=["GET", "POST", "HEAD"],
)
async def proxy_web_fetch(path: str, request: Request) -> Response:
    settings: Settings = request.app.state.settings
    return await _proxy(
        request,
        upstream_base_url=settings.web_fetch_gateway_url,
        path=path,
        required_scope="web_fetch.invoke",
        # web-fetch-gateway's _require_auth_context 403s unless X-Service-Scopes
        # contains web_fetch.invoke; the broker injects the trusted value.
        upstream_service_scope="web_fetch.invoke",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    # L1: the broker is reached only by untrusted sandboxes, so it must not
    # advertise its control-plane API.  Disabling the schema + docs endpoints
    # (``openapi_url=None`` also disables ``/docs`` and ``/redoc``, but we set
    # all three explicitly for clarity) stops a sandbox enumerating the whole
    # surface via ``/openapi.json``.  ``/healthz`` is a normal route and is
    # unaffected — only the schema endpoints go away.
    app = FastAPI(
        title="Tool Broker",
        version="0.1.0a1",
        description="Controlled sandbox façade for foundation tool services.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def _strip_server_header(request: Request, call_next):
        """L3: drop the ``Server`` header so the broker doesn't leak uvicorn.

        Works regardless of how the app is launched (uvicorn programmatically,
        gunicorn, a different ASGI server), so it is more robust than relying on
        ``server_header=False`` at the uvicorn call site.
        """
        response = await call_next(request)
        # Starlette's ``MutableHeaders`` supports ``__delitem__``/``__contains__``
        # but not ``pop``; delete the (case-insensitive) header only if present.
        if "server" in response.headers:
            del response.headers["server"]
        return response

    app.include_router(router, prefix=settings.api_prefix)
    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "tool_broker.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
