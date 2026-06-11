import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useConfirm } from '../components/dialogs';
import { CustomSelect, MultiChipSelect } from '../components/CustomSelect';
import type {
  McpServer,
  McpTransport,
  PatchMcpServerRequest,
  RegisterMcpServerRequest,
} from '../../api/types';

const TRANSPORTS: McpTransport[] = ['http', 'stdio'];

// ─── Register modal ───────────────────────────────────────────────────────────

function RegisterMcpModal({ onClose, onRegistered }: { onClose: () => void; onRegistered: (s: McpServer) => void }) {
  const [name, setName] = useState('');
  const [transport, setTransport] = useState<McpTransport>('http');
  const [endpoint, setEndpoint] = useState('');
  const [command, setCommand] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    const body: RegisterMcpServerRequest = {
      name: name.trim(),
      transport,
      enabled,
      scopes,
    };
    if (transport === 'http') body.endpoint = endpoint.trim() || null;
    if (transport === 'stdio') body.command = command.trim() || null;
    setLoading(true);
    setError(null);
    try {
      const created = await api.admin.mcp.register(body);
      onRegistered(created);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '注册失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onClose}>
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="border border-line rounded-md p-5 bg-surface flex flex-col gap-3 w-full max-w-md"
      >
        <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">注册 MCP 服务器</div>
        {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">名称</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-mcp-server"
            required
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">传输方式</label>
          <CustomSelect
            options={TRANSPORTS.map((t) => ({ value: t, label: t }))}
            value={transport}
            onChange={(v) => setTransport(v as McpTransport)}
          />
        </div>

        {transport === 'http' ? (
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">Endpoint</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              placeholder="https://host/mcp"
            />
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">Command</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder="python -m my_server"
            />
          </div>
        )}

        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Scopes</label>
          <MultiChipSelect options={['read', 'write', 'admin']} value={scopes} onChange={setScopes} />
        </div>

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
            {loading ? '注册中…' : '注册'}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink"
          >
            取消
          </button>
        </div>
      </form>
    </div>
  );
}

// ─── Edit row ─────────────────────────────────────────────────────────────────

function McpRow({
  server,
  onPatched,
  onDeleted,
}: {
  server: McpServer;
  onPatched: (s: McpServer) => void;
  onDeleted: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [endpoint, setEndpoint] = useState(server.endpoint ?? '');
  const [command, setCommand] = useState(server.command ?? '');
  const [enabled, setEnabled] = useState(server.enabled);
  const confirm = useConfirm();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const body: PatchMcpServerRequest = {};
    if ((endpoint || null) !== (server.endpoint ?? null)) body.endpoint = endpoint.trim() || null;
    if ((command || null) !== (server.command ?? null)) body.command = command.trim() || null;
    if (enabled !== server.enabled) body.enabled = enabled;
    if (Object.keys(body).length === 0) { setEditing(false); return; }
    setLoading(true);
    setError(null);
    try {
      const updated = await api.admin.mcp.patch(server.id, body);
      onPatched(updated);
      setEditing(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '更新失败');
    } finally {
      setLoading(false);
    }
  };

  const remove = async () => {
    if (!(await confirm({ title: '删除 MCP 服务器', message: `确认删除 MCP 服务器「${server.name}」？`, tone: 'danger', confirmText: '删除' }))) return;
    setLoading(true);
    setError(null);
    try {
      await api.admin.mcp.delete(server.id);
      onDeleted(server.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '删除失败');
      setLoading(false);
    }
  };

  if (!editing) {
    return (
      <tr className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
        <td className="px-3 py-2 text-[13px] text-ink">{server.name}</td>
        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{server.transport}</td>
        <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[220px]">
          {server.transport === 'http' ? (server.endpoint || '—') : (server.command || '—')}
        </td>
        <td className={`px-3 py-2 font-mono text-[11px] ${server.enabled ? 'text-ok' : 'text-dim'}`}>
          {server.enabled ? '已启用' : '已停用'}
        </td>
        <td className="px-3 py-2">
          <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
            {error && <span className="text-[11px] text-accent">{error}</span>}
            <button onClick={() => setEditing(true)} className="text-[11px] font-mono text-dim hover:text-ink">
              编辑
            </button>
            <button onClick={remove} disabled={loading} className="text-[11px] font-mono text-accent hover:opacity-80 disabled:opacity-50">
              删除
            </button>
          </div>
        </td>
      </tr>
    );
  }

  return (
    <tr className="border-b border-line bg-surface">
      <td className="px-3 py-2 text-[13px] text-ink">{server.name}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{server.transport}</td>
      <td className="px-3 py-2">
        {server.transport === 'http' ? (
          <input
            className="border border-line rounded-sm px-1.5 py-0.5 text-[12px] bg-paper focus:outline-none focus:border-accent w-full"
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="endpoint"
          />
        ) : (
          <input
            className="border border-line rounded-sm px-1.5 py-0.5 text-[12px] bg-paper focus:outline-none focus:border-accent w-full"
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            placeholder="command"
          />
        )}
      </td>
      <td className="px-3 py-2">
        <label className="flex items-center gap-1.5 text-[12px] text-ink-2">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          启用
        </label>
      </td>
      <td className="px-3 py-2 flex items-center gap-2">
        {error && <span className="text-[11px] text-accent">{error}</span>}
        <button onClick={save} disabled={loading} className="text-[11px] font-mono text-ok hover:opacity-80 disabled:opacity-50">
          {loading ? '…' : '保存'}
        </button>
        <button
          onClick={() => {
            setEditing(false);
            setEndpoint(server.endpoint ?? '');
            setCommand(server.command ?? '');
            setEnabled(server.enabled);
          }}
          className="text-[11px] font-mono text-dim hover:text-ink"
        >
          取消
        </button>
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

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    api.admin.mcp
      .list()
      .then((data) => { if (!cancelled) setServers(data); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setIsTodoEndpoint(true);
        } else {
          setError(err instanceof ApiError ? err.message : '加载失败');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const upsert = (s: McpServer) =>
    setServers((prev) => {
      const i = prev.findIndex((x) => x.id === s.id);
      if (i === -1) return [s, ...prev];
      const next = [...prev];
      next[i] = s;
      return next;
    });

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">MCP 服务器</h1>
          <p className="text-[12px] text-dim mt-0.5">注册与治理 MCP 服务器（→ mcp-registry）</p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 transition-opacity"
        >
          + 注册服务器
        </button>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力暂未启用：mcp-registry 代理端点尚未就绪，接线后将自动展示服务器列表。
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">加载中…</div>
      ) : !isTodoEndpoint && servers.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">暂无 MCP 服务器</div>
      ) : (
        !isTodoEndpoint && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">名称</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">传输</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">端点 / 命令</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                  <th className="px-3 py-2 w-32"></th>
                </tr>
              </thead>
              <tbody>
                {servers.map((s) => (
                  <McpRow
                    key={s.id}
                    server={s}
                    onPatched={upsert}
                    onDeleted={(id) => setServers((prev) => prev.filter((x) => x.id !== id))}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {showModal && <RegisterMcpModal onClose={() => setShowModal(false)} onRegistered={upsert} />}
    </div>
  );
}
