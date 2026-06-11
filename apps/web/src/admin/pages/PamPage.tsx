import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm, usePrompt, useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import type { AdminUser, PamElevation, PamSession } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN');
}

export function PamPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();
  const confirm = useConfirm();
  const prompt = usePrompt();

  const [pending, setPending] = useState<PamElevation[]>([]);
  const [history, setHistory] = useState<PamElevation[]>([]);
  const [sessions, setSessions] = useState<PamSession[]>([]);
  const [userMap, setUserMap] = useState<Map<string, string>>(new Map());
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);


  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    const tid = selectedTenantId ?? undefined;
    const usersQuery = tid ? `?tenant_id=${tid}` : '';
    Promise.all([
      api.admin.pam.pendingElevations(tid),
      api.admin.pam.elevationHistory(tid),
      api.admin.pam.sessions(tid),
      api.admin.users.list(usersQuery).catch(() => ({ items: [] as AdminUser[] })),
    ])
      .then(([p, h, s, u]) => {
        setPending(p); setHistory(h); setSessions(s);
        const m = new Map<string, string>();
        for (const usr of u.items) m.set(usr.id, usr.display_name || usr.email);
        setUserMap(m);
      })
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [selectedTenantId]);

  useEffect(() => { load(); }, [load, reloadKey]);

  const refresh = () => setReloadKey((k) => k + 1);
  const userName = (uid?: string) => (uid && userMap.get(uid)) || uid || '—';

  const handleApprove = async (id?: string) => {
    if (!id) return;
    const justification = await prompt({ title: '批准升权', label: '审批说明 (可选)', placeholder: '已核实工单' });
    if (justification === null) return;
    try {
      await api.admin.pam.approveElevation(id, { justification: justification.trim() }, selectedTenantId ?? undefined);
      toast.success('已批准');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '批准失败');
    }
  };

  const handleRevoke = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: '拒绝 / 吊销升权', message: '确定拒绝或吊销该升权请求？', tone: 'danger', confirmText: '确认' }))) return;
    try {
      await api.admin.pam.revokeElevation(id, selectedTenantId ?? undefined);
      toast.success('已处理');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '操作失败');
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">特权访问</h1>
        <p className="text-[12px] text-dim mt-0.5">升权审批、历史与活跃会话（→ pam）</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[{ value: '', label: '（默认 / 当前租户）' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {error && <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{error}</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：PAM 端点不可达，接线后将自动展示。</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {!todo && !loading && (
        <>
          {/* Pending */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">待审批升权</div>
            {pending.length === 0 ? (
              <div className="font-mono text-[12px] text-dim italic">暂无待审批请求</div>
            ) : (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">用户</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">理由</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                      <th className="px-3 py-2 w-32"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map((p, i) => (
                      <tr key={p.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{userName(p.user_id)}</td>
                        <td className="px-3 py-2 text-[12px] text-ink truncate max-w-[260px]">{p.reason ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-warn">{p.status ?? '—'}</td>
                        <td className="px-3 py-2 flex items-center gap-2">
                          <button onClick={() => handleApprove(p.id)} className="text-[11px] font-mono text-green-700 border border-green-200 rounded-sm px-2 py-0.5 hover:bg-green-50">批准</button>
                          <button onClick={() => handleRevoke(p.id)} className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50">拒绝</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Active sessions */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">活跃会话</div>
            {sessions.length === 0 ? (
              <div className="font-mono text-[12px] text-dim italic">暂无活跃升权会话</div>
            ) : (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">会话 ID</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">用户</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">权限</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">到期</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sessions.map((s, i) => (
                      <tr key={s.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[140px]">{s.id ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{userName(s.user_id)}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.permissions?.join(', ') || '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(s.expires_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* History */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">历史记录</div>
            {history.length === 0 ? (
              <div className="font-mono text-[12px] text-dim italic">暂无历史记录</div>
            ) : (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">用户</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">理由</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">申请时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((h, i) => (
                      <tr key={h.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{userName(h.user_id)}</td>
                        <td className="px-3 py-2 text-[12px] text-dim truncate max-w-[260px]">{h.reason ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{h.status ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(h.requested_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
