import { useState } from 'react';
import { api, ApiError } from '../api/client';
import { Dialog } from '../admin/components/Dialog';
import { CustomSelect } from '../admin/components/CustomSelect';
import type {
  McpAuthInput,
  McpAuthType,
  McpServer,
  McpTestResult,
  McpTestTool,
  McpTransport,
  PatchMcpServerRequest,
  RegisterMcpServerRequest,
  ToolFilter,
} from '../api/types';

// ─── Shared MCP server create / edit form dialog ──────────────────────────────
//
// Extracted from admin/pages/McpServersPage.tsx so the library "安装 MCP 工具"
// surface can reuse the exact same field set (transport / endpoint / headers /
// auth / timeout / tool_filter / enabled, plus stdio command/args/env) without
// duplicating the form. The dialog is transport-agnostic and API-agnostic:
//
//   * `onSubmit` receives the assembled register/patch body and returns the
//     saved server, so the caller decides whether to hit the admin governance
//     endpoint or the library install endpoint.
//   * `probe` (optional) supplies the tool-list discovery surface used by the
//     tool-filter editor. When omitted, the form falls back to its built-in
//     admin probe logic (id-less /probe + saved-server /test) which reads the
//     live form state. When provided (e.g. the library passes canFetch=false),
//     the supplied values replace the built-in ones so callers without a probe
//     endpoint get a manual tag-input fallback.

const TRANSPORTS: McpTransport[] = ['http', 'sse', 'stdio'];

/** Backend-returned masked sentinel for stored secret fields. Sending this
 *  value back in a PATCH tells the backend "keep the stored secret unchanged". */
const MASKED = '••••••'; // "••••••"

// Shared input / label class tokens so the extended form stays visually
// consistent with the rest of the admin console (matches ApiPlatformPage etc.).
const inputCls =
  'border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent';
const labelCls = 'text-[11px] text-dim font-mono';

// ─── Helpers: key-value pairs & free-form tag input ───────────────────────────

interface KvPair {
  key: string;
  value: string;
}

function toKv(rec: Record<string, string> | undefined | null): KvPair[] {
  if (!rec) return [];
  return Object.entries(rec).map(([key, value]) => ({ key, value }));
}

/** Drop pairs whose key is empty/whitespace; later duplicates win. */
function fromKv(pairs: KvPair[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const { key, value } of pairs) {
    const k = key.trim();
    if (!k) continue;
    out[k] = value;
  }
  return out;
}

function KeyValueEditor({
  pairs,
  onChange,
  keyPlaceholder = 'key',
  valuePlaceholder = 'value',
}: {
  pairs: KvPair[];
  onChange: (p: KvPair[]) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}) {
  const update = (i: number, patch: Partial<KvPair>) => {
    onChange(pairs.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  };
  return (
    <div className="flex flex-col gap-1.5">
      {pairs.map((p, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <input
            className={`${inputCls} flex-1 font-mono`}
            value={p.key}
            onChange={(e) => update(i, { key: e.target.value })}
            placeholder={keyPlaceholder}
          />
          <span className="text-dim text-[12px] font-mono">=</span>
          <input
            className={`${inputCls} flex-1 font-mono`}
            value={p.value}
            onChange={(e) => update(i, { value: e.target.value })}
            placeholder={valuePlaceholder}
          />
          <button
            type="button"
            onClick={() => onChange(pairs.filter((_, idx) => idx !== i))}
            className="text-[14px] leading-none text-dim hover:text-accent px-1"
            aria-label="删除该行"
          >
            ×
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => onChange([...pairs, { key: '', value: '' }])}
        className="self-start text-[11px] font-mono text-dim hover:text-ink border border-dashed border-line rounded-sm px-2 py-0.5"
      >
        + 添加一行
      </button>
    </div>
  );
}

/** Free-form tag input. Typing Enter or the configured separator commits the
 *  draft (splitting on the separator so paste / space-separated input works);
 *  Backspace on an empty draft removes the last chip. */
function TagInput({
  value,
  onChange,
  placeholder,
  separator = ',',
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  separator?: string;
}) {
  const [draft, setDraft] = useState('');
  const splitRe = new RegExp(`[${separator}\\n]`);

  const commit = (raw: string) => {
    const parts = raw.split(splitRe).map((s) => s.trim()).filter(Boolean);
    if (parts.length === 0) return;
    const merged = [...value];
    for (const p of parts) if (!merged.includes(p)) merged.push(p);
    onChange(merged);
    setDraft('');
  };

  return (
    <div className="flex flex-col gap-1">
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {value.map((v) => (
            <span
              key={v}
              className="inline-flex items-center gap-0.5 bg-accent-soft border border-accent rounded-full px-1.5 py-px text-[11px] font-mono text-ink"
            >
              {v}
              <button
                type="button"
                onClick={() => onChange(value.filter((x) => x !== v))}
                className="text-dim hover:text-ink ml-0.5"
                aria-label={`移除 ${v}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        className={`${inputCls} font-mono`}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === separator) {
            e.preventDefault();
            commit(draft);
          } else if (e.key === 'Backspace' && draft === '' && value.length > 0) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={() => {
          if (draft.trim()) commit(draft);
        }}
        placeholder={placeholder ?? '输入后回车添加'}
      />
    </div>
  );
}

// ─── Tool filter editor ───────────────────────────────────────────────────────
//
// The wire format stays `tool_filter: {allow, block}`, but the UI expresses it
// as one mode + one checklist: typing tool names by hand was silently
// error-prone (a typo in `allow` blocks the whole server). Tool names come from
// a live probe of the config as currently typed (the id-less
// `/servers/probe`), so editing an endpoint and re-fetching works without
// saving first.
//
// Two degraded paths must keep working:
//   * probe unavailable / failed / endpoint not filled in yet -> fall back to
//     the free-form TagInput so the server can still be configured;
//   * already-configured names missing from the probe result (upstream removed
//     a tool, or the name was hand-typed) -> still rendered as checked rows,
//     flagged, and preserved on save.

type ToolFilterMode = 'none' | 'allow' | 'block';

const TOOL_FILTER_MODES: { value: ToolFilterMode; label: string }[] = [
  { value: 'none', label: '不限制（全部工具可用）' },
  { value: 'allow', label: '仅允许勾选的（白名单）' },
  { value: 'block', label: '禁用勾选的（黑名单）' },
];

interface DisplayTool extends McpTestTool {
  /** True when the name is configured but absent from the latest probe. */
  missing: boolean;
}

function ToolFilterEditor({
  mode,
  onModeChange,
  selected,
  onSelectedChange,
  fetchTools,
  canFetch,
  fetchDisabledTitle,
  conflict,
}: {
  mode: ToolFilterMode;
  onModeChange: (m: ToolFilterMode) => void;
  selected: string[];
  onSelectedChange: (v: string[]) => void;
  fetchTools: () => Promise<McpTestResult>;
  canFetch: boolean;
  fetchDisabledTitle: string;
  conflict: { allow: string[]; block: string[] } | null;
}) {
  const [tools, setTools] = useState<McpTestTool[] | null>(null);
  const [fetching, setFetching] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  // Escape hatch: even with a probe result the admin may want to type a name
  // the server does not advertise (yet).
  const [manual, setManual] = useState(false);

  const run = async () => {
    setFetching(true);
    setFetchError(null);
    try {
      const r = await fetchTools();
      if (!r.ok) {
        setTools(null);
        setFetchError(r.error || '探测失败：上游未返回工具列表');
        return;
      }
      setTools(r.tools ?? []);
      setManual(false);
    } catch (err) {
      setTools(null);
      setFetchError(err instanceof ApiError ? err.message : '拉取工具列表失败');
    } finally {
      setFetching(false);
    }
  };

  const fetched = tools ?? [];
  const known = new Set(fetched.map((t) => t.name));
  const displayTools: DisplayTool[] = [
    ...fetched.map((t) => ({ ...t, missing: false })),
    ...selected.filter((n) => !known.has(n)).map((n) => ({ name: n, missing: true })),
  ];
  const showChecklist = !manual && tools !== null && displayTools.length > 0;

  const toggle = (name: string, on: boolean) =>
    onSelectedChange(on ? [...selected, name] : selected.filter((n) => n !== name));

  return (
    <div className="flex flex-col gap-1.5">
      <label className={labelCls}>工具过滤</label>
      <CustomSelect
        options={TOOL_FILTER_MODES}
        value={mode}
        onChange={(v) => onModeChange(v as ToolFilterMode)}
      />

      {mode === 'none' ? (
        <span className="text-[11px] text-dim italic">该服务器的全部工具都可被调用。</span>
      ) : (
        <>
          {conflict && (
            <div className="text-[11px] text-accent bg-accent-soft border border-accent-line rounded-sm px-2 py-1 leading-relaxed">
              该服务器同时配置了白名单与黑名单（allow：{conflict.allow.join('、')}；block：
              {conflict.block.join('、')}）。已按白名单模式载入，保存后只会保留当前模式对应的列表，另一侧将被清空。
            </div>
          )}

          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              onClick={run}
              disabled={!canFetch || fetching}
              title={canFetch ? '连接该服务器并列出它提供的工具' : fetchDisabledTitle}
              className="text-[11px] font-mono border border-line rounded-sm px-2 py-1 text-ink-2 hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {fetching ? '拉取中…' : '拉取工具列表'}
            </button>
            <span className="text-[11px] text-dim font-mono">已选 {selected.length}</span>
            {tools !== null && (
              <button
                type="button"
                onClick={() => setManual((m) => !m)}
                className="text-[11px] font-mono text-dim hover:text-ink"
              >
                {manual ? '改为勾选' : '手动输入'}
              </button>
            )}
            {showChecklist && (
              <>
                <button
                  type="button"
                  onClick={() => onSelectedChange(displayTools.map((t) => t.name))}
                  className="text-[11px] font-mono text-dim hover:text-ink"
                >
                  全选
                </button>
                <button
                  type="button"
                  onClick={() => onSelectedChange([])}
                  className="text-[11px] font-mono text-dim hover:text-ink"
                >
                  清空
                </button>
              </>
            )}
          </div>

          {fetchError && (
            <div className="text-[11px] text-accent font-mono break-all bg-accent-soft rounded-sm px-2 py-1">
              {fetchError}
              <div className="text-[10px] text-dim font-sans mt-0.5 not-italic">
                无法拉取时可直接手动输入工具名，配置照常生效。
              </div>
            </div>
          )}

          {tools !== null && fetched.length === 0 && !fetchError && (
            <span className="text-[11px] text-dim italic">
              上游未返回任何工具，可手动输入工具名。
            </span>
          )}

          {showChecklist ? (
            <div className="flex flex-col gap-0.5 max-h-[220px] overflow-auto border border-line rounded-sm p-1.5 bg-paper">
              {displayTools.map((t) => (
                <label
                  key={t.name}
                  className="flex items-start gap-2 px-1 py-0.5 rounded-sm hover:bg-paper-2 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    className="mt-[3px] shrink-0"
                    checked={selected.includes(t.name)}
                    onChange={(e) => toggle(t.name, e.target.checked)}
                  />
                  <span className="flex flex-col min-w-0 flex-1">
                    <span className="text-[12px] font-mono text-ink truncate">
                      {t.name}
                      {t.missing && (
                        <span className="ml-1.5 text-[10px] font-sans text-accent">
                          未在当前工具列表中
                        </span>
                      )}
                    </span>
                    {t.description && (
                      <span className="text-[11px] text-dim truncate" title={t.description}>
                        {t.description}
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
          ) : (
            <TagInput
              value={selected}
              onChange={onSelectedChange}
              placeholder="工具名，回车或逗号分隔"
            />
          )}
        </>
      )}
    </div>
  );
}

// ─── Shared create / edit form dialog ─────────────────────────────────────────
//
// One form drives both register and patch. name + transport are immutable after
// creation (PatchMcpServerRequest has neither), so they render disabled in edit
// mode. Transport-gated fields: http/sse -> endpoint / headers / auth; stdio ->
// command / args / env. timeout / tool_filter / enabled apply to all.
// sse shares http's endpoint/headers/auth shape (legacy SSE dual-endpoint);
// only the SDK worker direct path can actually connect to sse servers.

/** Optional tool-list discovery surface. When omitted the form uses its
 *  built-in admin probe (id-less /probe + saved-server /test) which reads the
 *  live form state; when provided the supplied values replace it (e.g. the
 *  library install path passes canFetch=false for a manual tag-input fallback). */
export interface McpServerFormProbe {
  fetchTools: () => Promise<McpTestResult>;
  canFetch: boolean;
  disabledTitle: string;
}

export function McpServerFormDialog({
  mode,
  server,
  onClose,
  onSaved,
  onSubmit,
  probe,
  title,
  submitLabel,
}: {
  mode: 'create' | 'edit';
  server?: McpServer;
  onClose: () => void;
  onSaved: (s: McpServer) => void;
  /** Receives the assembled register/patch body and returns the saved server.
   *  The caller routes to its own API (admin register/patch or library install). */
  onSubmit: (body: RegisterMcpServerRequest | PatchMcpServerRequest) => Promise<McpServer>;
  /** Optional tool-list discovery surface; see McpServerFormProbe. */
  probe?: McpServerFormProbe;
  /** Override the dialog title (defaults to "注册/编辑 MCP 服务器"). */
  title?: string;
  /** Override the submit button label (defaults to "注册"/"保存"). */
  submitLabel?: string;
}) {
  const isEdit = mode === 'edit';
  const [name, setName] = useState(server?.name ?? '');
  const [transport, setTransport] = useState<McpTransport>(server?.transport ?? 'http');
  const [endpoint, setEndpoint] = useState(server?.endpoint ?? '');
  const [command, setCommand] = useState(server?.command ?? '');
  const [args, setArgs] = useState<string[]>(server?.args ?? []);
  const [envPairs, setEnvPairs] = useState<KvPair[]>(toKv(server?.env));
  const [headersPairs, setHeadersPairs] = useState<KvPair[]>(toKv(server?.headers));
  const [authType, setAuthType] = useState<McpAuthType>(server?.auth?.type ?? 'none');
  const [authToken, setAuthToken] = useState(server?.auth?.token ?? '');
  const [authHeaderName, setAuthHeaderName] = useState(server?.auth?.header_name ?? 'X-API-Key');
  const [authApiKey, setAuthApiKey] = useState(server?.auth?.api_key ?? '');
  const [authOauthClientId, setAuthOauthClientId] = useState(server?.auth?.client_id ?? '');
  const [authOauthClientSecret, setAuthOauthClientSecret] = useState(server?.auth?.client_secret ?? '');
  const [authOauthTokenUrl, setAuthOauthTokenUrl] = useState(server?.auth?.token_url ?? '');
  const [authOauthScopes, setAuthOauthScopes] = useState<string[]>(server?.auth?.scopes ?? []);
  const [timeoutSeconds, setTimeoutSeconds] = useState<string>(
    server?.timeout_seconds != null ? String(server.timeout_seconds) : '',
  );
  // tool_filter is stored as {allow, block} but edited as mode + one list.
  // Reverse-derive the mode: allow wins, then block, else unrestricted. When a
  // legacy row carries both, we surface a warning instead of silently dropping
  // the losing side (see `filterConflict`).
  const initialAllow = server?.tool_filter?.allow ?? [];
  const initialBlock = server?.tool_filter?.block ?? [];
  const [filterMode, setFilterMode] = useState<ToolFilterMode>(
    initialAllow.length > 0 ? 'allow' : initialBlock.length > 0 ? 'block' : 'none',
  );
  const [filterTools, setFilterTools] = useState<string[]>(
    initialAllow.length > 0 ? initialAllow : initialBlock,
  );
  const filterConflict =
    initialAllow.length > 0 && initialBlock.length > 0
      ? { allow: initialAllow, block: initialBlock }
      : null;
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // http and sse share the endpoint / headers / auth shape; stdio uses
  // command / args / env. sse is only consumable by the SDK worker direct
  // path, but the form still captures the same fields as http.
  const isHttpLike = transport === 'http' || transport === 'sse';

  const buildAuth = (): McpAuthInput => {
    if (authType === 'none') return { type: 'none' };
    if (authType === 'bearer') return { type: 'bearer', token: authToken.trim() || null };
    if (authType === 'oauth') {
      return {
        type: 'oauth',
        client_id: authOauthClientId.trim() || null,
        client_secret: authOauthClientSecret.trim() || null,
        token_url: authOauthTokenUrl.trim() || null,
        scopes: authOauthScopes,
      };
    }
    return {
      type: 'api_key',
      header_name: authHeaderName.trim() || 'X-API-Key',
      api_key: authApiKey.trim() || null,
    };
  };

  /** null = leave at backend default; invalid input is treated as unset here
   *  (submit() reports it as a validation error before saving). */
  const parseTimeout = (): number | null => {
    const raw = timeoutSeconds.trim();
    if (raw === '') return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  };

  // Returns true when the auth secret field(s) for the current type still hold
  // the masked sentinel, meaning the admin has not retyped the credential.
  const authHasMasked = (): boolean => {
    if (authType === 'bearer') return authToken === MASKED;
    if (authType === 'api_key') return authApiKey === MASKED;
    if (authType === 'oauth') return authOauthClientSecret === MASKED;
    return false;
  };

  // Built-in admin probe: in edit mode with masked (unchanged) secrets, probe
  // via the saved-server /test endpoint which decrypts the stored secret
  // server-side. In create mode (or when the admin has retyped the secret) the
  // id-less /probe with the plaintext auth from the form is used instead.
  // Only used when no explicit `probe` prop is supplied.
  const builtInProbeTools = (): Promise<McpTestResult> => {
    if (isEdit && server && authHasMasked()) {
      // Secret fields still masked - use the stored-secret path with optional
      // overrides for non-secret fields the admin may have edited.
      return api.admin.mcp.test(server.id, {
        endpoint: endpoint.trim() || undefined,
        headers: fromKv(headersPairs),
        timeout_seconds: parseTimeout() ?? undefined,
      });
    }
    return api.admin.mcp.probe({
      transport,
      endpoint: endpoint.trim() || null,
      headers: fromKv(headersPairs),
      auth: buildAuth(),
      timeout_seconds: parseTimeout(),
    });
  };

  // Only http can be probed: sse is worker-direct only and stdio needs a local
  // process (mirrors the row-level canTest gate).
  const needsEndpoint = endpoint.trim() === '';
  const builtInCanProbe = transport === 'http' && !needsEndpoint;
  const builtInProbeDisabledTitle =
    transport === 'sse'
      ? 'sse 仅在 worker 直连模式可用，注册探测器暂不支持'
      : transport === 'stdio'
        ? 'stdio 需本地进程，暂不支持探测；请手动输入工具名'
        : '请先填写 Endpoint';

  // Caller-supplied probe wins over the built-in admin logic.
  const effFetchTools = probe?.fetchTools ?? builtInProbeTools;
  const effCanFetch = probe ? probe.canFetch : builtInCanProbe;
  const effDisabledTitle = probe ? probe.disabledTitle : builtInProbeDisabledTitle;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    const trimmedTimeout = timeoutSeconds.trim();
    const timeoutNum = trimmedTimeout === '' ? null : Number(trimmedTimeout);
    if (timeoutNum != null && !Number.isFinite(timeoutNum)) {
      setError('超时秒数需为数字');
      return;
    }
    if (timeoutNum != null && timeoutNum <= 0) {
      setError('超时秒数需大于 0');
      return;
    }
    // Map mode + checklist back onto the wire shape; the non-active side is
    // always cleared so the saved filter matches what the form showed.
    const toolFilter: ToolFilter =
      filterMode === 'allow'
        ? { allow: filterTools, block: [] }
        : filterMode === 'block'
          ? { allow: [], block: filterTools }
          : { allow: [], block: [] };

    setLoading(true);
    setError(null);
    try {
      let saved: McpServer;
      if (isEdit) {
        const body: PatchMcpServerRequest = {
          enabled,
          timeout_seconds: timeoutNum,
          tool_filter: toolFilter,
        };
        if (isHttpLike) {
          body.endpoint = endpoint.trim() || null;
          body.headers = fromKv(headersPairs);
          body.auth = buildAuth();
        } else {
          body.command = command.trim() || null;
          body.args = args;
          body.env = fromKv(envPairs);
        }
        saved = await onSubmit(body);
      } else {
        const body: RegisterMcpServerRequest = {
          name: name.trim(),
          transport,
          enabled,
          timeout_seconds: timeoutNum,
          tool_filter: toolFilter,
        };
        if (isHttpLike) {
          body.endpoint = endpoint.trim() || null;
          body.headers = fromKv(headersPairs);
          body.auth = buildAuth();
        } else {
          body.command = command.trim() || null;
          body.args = args;
          body.env = fromKv(envPairs);
        }
        saved = await onSubmit(body);
      }
      onSaved(saved);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : isEdit ? '更新失败' : '注册失败');
    } finally {
      setLoading(false);
    }
  };

  const dialogTitle = title ?? (isEdit ? '编辑 MCP 服务器' : '注册 MCP 服务器');
  const submitText = submitLabel ?? (isEdit ? '保存' : '注册');

  return (
    <Dialog
      title={dialogTitle}
      onClose={onClose}
      dismissable={!loading}
      className="w-full max-w-lg max-h-[88vh] overflow-auto"
    >
      <form onSubmit={submit} className="flex flex-col gap-3">
        {error && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
        )}

        <div className="flex flex-col gap-1">
          <label className={labelCls}>名称</label>
          <input
            className={inputCls}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-mcp-server"
            required
            disabled={isEdit}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>传输方式</label>
          <CustomSelect
            options={TRANSPORTS.map((t) => ({ value: t, label: t }))}
            value={transport}
            onChange={(v) => setTransport(v as McpTransport)}
          />
          {isEdit && <span className="text-[10px] text-dim italic">传输方式创建后不可修改</span>}
        </div>

        {isHttpLike ? (
          <>
            <div className="flex flex-col gap-1">
              <label className={labelCls}>Endpoint</label>
              <input
                className={inputCls}
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
                placeholder="https://host/mcp"
              />
            </div>

            <div className="flex flex-col gap-1">
              <label className={labelCls}>自定义 Headers</label>
              <KeyValueEditor pairs={headersPairs} onChange={setHeadersPairs} />
            </div>

            <div className="flex flex-col gap-1">
              <label className={labelCls}>鉴权</label>
              <CustomSelect
                options={[
                  { value: 'none', label: '无' },
                  { value: 'bearer', label: 'Bearer Token' },
                  { value: 'api_key', label: 'API Key' },
                  { value: 'oauth', label: 'OAuth' },
                ]}
                value={authType}
                onChange={(v) => setAuthType(v as McpAuthType)}
              />
              {authType === 'bearer' && (
                <input
                  className={`${inputCls} font-mono`}
                  value={authToken}
                  onChange={(e) => setAuthToken(e.target.value)}
                  placeholder="bearer token"
                  type="password"
                />
              )}
              {authType === 'api_key' && (
                <div className="flex flex-col gap-1">
                  <input
                    className={`${inputCls} font-mono`}
                    value={authHeaderName}
                    onChange={(e) => setAuthHeaderName(e.target.value)}
                    placeholder="X-API-Key"
                  />
                  <input
                    className={`${inputCls} font-mono`}
                    value={authApiKey}
                    onChange={(e) => setAuthApiKey(e.target.value)}
                    placeholder="api key"
                    type="password"
                  />
                </div>
              )}
              {authType === 'oauth' && (
                <div className="flex flex-col gap-1">
                  <input
                    className={`${inputCls} font-mono`}
                    value={authOauthClientId}
                    onChange={(e) => setAuthOauthClientId(e.target.value)}
                    placeholder="client_id"
                  />
                  <input
                    className={`${inputCls} font-mono`}
                    value={authOauthClientSecret}
                    onChange={(e) => setAuthOauthClientSecret(e.target.value)}
                    placeholder="client_secret"
                    type="password"
                  />
                  <input
                    className={`${inputCls} font-mono`}
                    value={authOauthTokenUrl}
                    onChange={(e) => setAuthOauthTokenUrl(e.target.value)}
                    placeholder="https://token-url"
                  />
                  <TagInput
                    value={authOauthScopes}
                    onChange={setAuthOauthScopes}
                    placeholder="scope，回车或逗号分隔"
                  />
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            <div className="flex flex-col gap-1">
              <label className={labelCls}>Command</label>
              <input
                className={inputCls}
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="python"
              />
            </div>

            <div className="flex flex-col gap-1">
              <label className={labelCls}>Args</label>
              <TagInput
                value={args}
                onChange={setArgs}
                separator=" "
                placeholder="输入参数后回车或空格分隔"
              />
            </div>

            <div className="flex flex-col gap-1">
              <label className={labelCls}>Env</label>
              <KeyValueEditor pairs={envPairs} onChange={setEnvPairs} />
            </div>
          </>
        )}

        <div className="flex flex-col gap-1">
          <label className={labelCls}>超时（秒，留空 = 默认）</label>
          <input
            type="number"
            min={1}
            className={`${inputCls} font-mono`}
            value={timeoutSeconds}
            onChange={(e) => setTimeoutSeconds(e.target.value)}
            placeholder="如 30"
          />
        </div>

        <ToolFilterEditor
          mode={filterMode}
          onModeChange={setFilterMode}
          selected={filterTools}
          onSelectedChange={setFilterTools}
          fetchTools={effFetchTools}
          canFetch={effCanFetch}
          fetchDisabledTitle={effDisabledTitle}
          conflict={filterConflict}
        />

        <label className="flex items-center gap-2 text-[12px] text-ink-2">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          启用
        </label>

        <div className="flex gap-2 pt-1">
          <button
            type="submit"
            disabled={loading}
            className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? (isEdit ? '保存中…' : '注册中…') : submitText}
          </button>
          <button
            type="button"
            onClick={onClose}
            disabled={loading}
            className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
          >
            取消
          </button>
        </div>
      </form>
    </Dialog>
  );
}
