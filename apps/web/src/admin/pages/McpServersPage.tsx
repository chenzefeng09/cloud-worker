import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useConfirm } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import type {
  McpServer,
  McpTestResult,
  McpTestTool,
  PatchMcpServerRequest,
  RegisterMcpServerRequest,
} from '../../api/types';
import { McpServerFormDialog } from '../../components/McpServerFormDialog';

// ─── Tools / connectivity dialog ──────────────────────────────────────────────
//
// Same probe as before, but presented tool-list-first: this is the admin's way
// to see what a registered server actually exposes (the registry does not
// persist tool lists, so every open re-probes live).

function McpToolsDialog({
  server,
  onClose,
  onTested,
}: {
  server: McpServer;
  onClose: () => void;
  /** Called once with the probe result's tool list when the test succeeds.
   *  Parent uses this to write `discovered_tools` back onto the row so the
   *  table's 工具 column updates without a full reload. */
  onTested?: (tools: McpTestTool[]) => void;
}) {
  const [loading, setLoading] = useState(true);
  const [result, setResult] = useState<McpTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.admin.mcp
      .test(server.id)
      .then((r) => {
        if (cancelled) return;
        setResult(r);
        // Write the discovered tool list back to the parent on success so the
        // table's 工具 column reflects it immediately after the dialog closes.
        if (r.ok && r.tools && onTested) onTested(r.tools);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to fetch tools');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // onTested is intentionally omitted: it is an inline closure from the
    // parent and would otherwise re-trigger this effect (and the /test call)
    // on every parent re-render. The probe should fire once per server.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [server.id]);

  return (
    <Dialog title={`Tools: ${server.name}`} onClose={onClose} className="w-full max-w-lg">
      <div className="flex flex-col gap-3">
        {loading && <div className="font-mono text-[12px] text-dim">Connecting and reading tools…</div>}

        {error && !loading && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
        )}

        {result && !loading && (
          <>
            {result.tools && result.tools.length > 0 ? (
              <div className="flex flex-col gap-1">
                <div className="text-[11px] text-dim font-mono uppercase tracking-wide">
                  Tools ({result.tools.length})
                </div>
                <div className="flex flex-col gap-1 max-h-[320px] overflow-auto">
                  {result.tools.map((t) => (
                    <div
                      key={t.name}
                      className="border border-line rounded-sm px-2 py-1 bg-paper"
                    >
                      <div className="text-[12px] font-mono text-ink break-all">{t.name}</div>
                      {t.description && (
                        <div className="text-[11px] text-dim mt-0.5 whitespace-pre-wrap break-words">
                          {t.description}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="text-[12px] text-dim italic">
                {result.ok ? 'This server returned no tools.' : 'Couldn’t read the tool list.'}
              </div>
            )}

            <div
              className={`text-[12px] font-mono px-2 py-1 rounded-sm border ${
                result.ok
                  ? 'bg-ok/10 border-ok text-ok'
                  : 'bg-accent-soft border-accent-line text-accent'
              }`}
            >
              {result.ok ? '✓ Connection OK' : '✗ Connection failed'}
            </div>

            {result.error && (
              <div className="text-[12px] text-accent font-mono break-all">{result.error}</div>
            )}

            {result.server_info && Object.keys(result.server_info).length > 0 && (
              <div className="flex flex-col gap-1">
                <div className="text-[11px] text-dim font-mono uppercase tracking-wide">
                  Server info
                </div>
                <pre className="text-[11px] font-mono text-ink-2 bg-paper border border-line rounded-sm px-2 py-1.5 overflow-auto max-h-[160px] whitespace-pre-wrap break-all">
                  {JSON.stringify(result.server_info, null, 2)}
                </pre>
              </div>
            )}
          </>
        )}

        <div className="flex justify-end pt-1">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink"
          >
            Close
          </button>
        </div>
      </div>
    </Dialog>
  );
}

// ─── Row ──────────────────────────────────────────────────────────────────────

/** Renders a server's discovered tool list inline. null/empty -> "Not probed".
 *  Shows up to N names as tags, folding the rest into a "+K" overflow chip
 *  whose tooltip lists the remaining names. Reads only from
 *  `server.discovered_tools` - never probes at render time. */
const TOOL_PREVIEW_LIMIT = 5;

function ToolsCell({ tools }: { tools?: McpTestTool[] | null }) {
  if (!tools || tools.length === 0) {
    return <span className="text-[11px] text-dim italic">Not probed</span>;
  }
  const visible = tools.slice(0, TOOL_PREVIEW_LIMIT);
  const overflow = tools.length - visible.length;
  return (
    <div className="flex flex-wrap items-center gap-1 max-w-[280px]">
      {visible.map((t) => (
        <span
          key={t.name}
          title={t.description ?? undefined}
          className="inline-block bg-accent-soft border border-accent-line rounded-full px-1.5 py-px text-[10.5px] font-mono text-ink-2 truncate max-w-[140px]"
        >
          {t.name}
        </span>
      ))}
      {overflow > 0 && (
        <span
          title={`${tools.length} tools: ${tools
            .slice(TOOL_PREVIEW_LIMIT)
            .map((t) => t.name)
            .join(', ')}`}
          className="text-[10.5px] font-mono text-dim"
        >
          +{overflow}
        </span>
      )}
    </div>
  );
}

function McpRow({
  server,
  onEdit,
  onTest,
  onDeleted,
}: {
  server: McpServer;
  onEdit: (s: McpServer) => void;
  onTest: (s: McpServer) => void;
  onDeleted: (id: string) => void;
}) {
  const confirm = useConfirm();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remove = async () => {
    if (
      !(await confirm({
        title: 'Delete MCP server',
        message: `Delete MCP server “${server.name}”?`,
        tone: 'danger',
        confirmText: 'Delete',
      }))
    )
      return;
    setLoading(true);
    setError(null);
    try {
      await api.admin.mcp.delete(server.id);
      onDeleted(server.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to delete');
      setLoading(false);
    }
  };

  // Only http is probeable via the registry tester. sse uses the legacy
  // dual-endpoint transport that only the SDK worker path supports natively,
  // and stdio needs a local process - both are disabled here.
  const canTest = server.transport === 'http';
  const testDisabledTitle =
    server.transport === 'sse'
      ? 'SSE is only available in worker direct-connect mode; the registry tester doesn’t support it yet'
      : 'Reading tools remotely isn’t supported for stdio yet';

  return (
    <tr className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
      <td className="px-3 py-2 text-[13px] text-ink">{server.name}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{server.transport}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[220px]">
        {server.transport === 'http' || server.transport === 'sse'
          ? server.endpoint || '-'
          : server.command || '-'}
      </td>
      <td className={`px-3 py-2 font-mono text-[11px] ${server.enabled ? 'text-ok' : 'text-dim'}`}>
        {server.enabled ? 'Enabled' : 'Disabled'}
      </td>
      <td className="px-3 py-2">
        <ToolsCell tools={server.discovered_tools} />
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
          {error && <span className="text-[11px] text-accent">{error}</span>}
          <button
            onClick={() => onTest(server)}
            disabled={!canTest || loading}
            title={canTest ? 'View the tools this server provides and check connectivity' : testDisabledTitle}
            className="text-[11px] font-mono text-dim hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Test connection
          </button>
          <button
            onClick={() => onEdit(server)}
            disabled={loading}
            className="text-[11px] font-mono text-dim hover:text-ink disabled:opacity-50"
          >
            Edit
          </button>
          <button
            onClick={remove}
            disabled={loading}
            className="text-[11px] font-mono text-accent hover:opacity-80 disabled:opacity-50"
          >
            Delete
          </button>
        </div>
      </td>
    </tr>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function McpServersPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [showModal, setShowModal] = useState(false);
  // Editing / testing are page-level so the Dialogs (fixed-position <div>s)
  // render outside the <tbody>; rendering them inside a row would be invalid
  // HTML and the browser parser would hoist them out of the table.
  const [editingServer, setEditingServer] = useState<McpServer | null>(null);
  const [inspectingServer, setInspectingServer] = useState<McpServer | null>(null);

  // Per-session record of server ids we've already auto-probed (background
  // fill of discovered_tools). Prevents re-probing failing servers every
  // time the list reloads - the admin can still force a refresh via the
  // 连通性测试 button. Resets on component remount (new "session").
  const autoProbedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    api.admin.mcp
      .list()
      .then((data) => {
        if (cancelled) return;
        // Render the list immediately with whatever we got - empty-tool
        // rows show "未探测" right away. Then kick off best-effort
        // background probes for enabled http servers whose
        // discovered_tools is still empty, so the 工具 column fills in
        // without blocking the table. Tracked per-session: a server that
        // already failed once isn't auto-retried on the next list refresh
        // (manual 连通性测试 still forces a refresh).
        setServers(data);
        for (const s of data) {
          if (
            s.enabled &&
            s.transport === 'http' &&
            (!s.discovered_tools || s.discovered_tools.length === 0) &&
            !autoProbedRef.current.has(s.id)
          ) {
            autoProbedRef.current.add(s.id);
            refreshToolsInBackground(s);
          }
        }
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setIsTodoEndpoint(true);
        } else {
          setError(err instanceof ApiError ? err.message : 'Failed to load');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const upsert = (s: McpServer) =>
    setServers((prev) => {
      const i = prev.findIndex((x) => x.id === s.id);
      if (i === -1) return [s, ...prev];
      const next = [...prev];
      next[i] = s;
      return next;
    });

  // Best-effort background probe: after creating (or whenever we want to
  // refresh a row's tool list without opening the dialog) fire a /test and
  // write discovered_tools back. Silent on failure - the row just stays
  // "未探测" and the admin can use the explicit 连通性测试 button.
  const refreshToolsInBackground = (s: McpServer) => {
    if (!s.enabled || s.transport !== 'http') return;
    api.admin.mcp
      .test(s.id)
      .then((r) => {
        if (r.ok && r.tools) {
          setServers((prev) =>
            prev.map((x) => (x.id === s.id ? { ...x, discovered_tools: r.tools! } : x)),
          );
        }
      })
      .catch(() => {
        /* silent - see comment above */
      });
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">MCP servers</h1>
          <p className="text-[12px] text-dim mt-0.5">Register and govern MCP servers (→ mcp-registry)</p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 transition-opacity"
        >
          + Register server
        </button>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Not available yet: the mcp-registry proxy endpoint isn’t ready. Servers will appear here once it’s connected.
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">Loading…</div>
      ) : !isTodoEndpoint && servers.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No MCP servers</div>
      ) : (
        !isTodoEndpoint && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Name</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Transport</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Endpoint / command</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Tools</th>
                  <th className="px-3 py-2 w-52"></th>
                </tr>
              </thead>
              <tbody>
                {servers.map((s) => (
                  <McpRow
                    key={s.id}
                    server={s}
                    onEdit={setEditingServer}
                    onTest={setInspectingServer}
                    onDeleted={(id) => setServers((prev) => prev.filter((x) => x.id !== id))}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {showModal && (
        <McpServerFormDialog
          mode="create"
          onClose={() => setShowModal(false)}
          onSubmit={(body) => api.admin.mcp.register(body as RegisterMcpServerRequest)}
          onSaved={(s) => {
            upsert(s);
            // Auto-fill the 工具 column for freshly-created enabled http
            // servers so the admin doesn't have to click 连通性测试 right away.
            // Mark as auto-probed so a subsequent list reload doesn't fire
            // a duplicate probe (refreshToolsInBackground already filters
            // enabled+http internally; this just keeps the ref consistent).
            autoProbedRef.current.add(s.id);
            refreshToolsInBackground(s);
          }}
        />
      )}

      {editingServer && (
        <McpServerFormDialog
          mode="edit"
          server={editingServer}
          onClose={() => setEditingServer(null)}
          onSubmit={(body) => api.admin.mcp.patch(editingServer.id, body as PatchMcpServerRequest)}
          onSaved={(s) => {
            upsert(s);
            setEditingServer(null);
          }}
        />
      )}

      {inspectingServer && (
        <McpToolsDialog
          server={inspectingServer}
          onClose={() => setInspectingServer(null)}
          onTested={(tools) => {
            // Local upsert: preserve the latest row state (other fields may
            // have changed since the dialog opened) and only overwrite
            // discovered_tools.
            setServers((prev) =>
              prev.map((s) =>
                s.id === inspectingServer.id ? { ...s, discovered_tools: tools } : s,
              ),
            );
          }}
        />
      )}
    </div>
  );
}
