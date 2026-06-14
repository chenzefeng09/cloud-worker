import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useChatStore } from '../../stores/chatStore';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type { LegalHold, RecycleBinItem } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

export function LifecyclePage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const actorId = useChatStore((s) => s.userProfile?.id) ?? 'admin';
  const toast = useToast();
  const confirm = useConfirm();
  const tid = selectedTenantId ?? undefined;

  const [items, setItems] = useState<RecycleBinItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [holds, setHolds] = useState<LegalHold[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // Soft-delete form.
  const [sdOpen, setSdOpen] = useState(false);
  const [sdTypeId, setSdTypeId] = useState('');
  const [sdResId, setSdResId] = useState('');
  const [sdName, setSdName] = useState('');
  const [sdBusy, setSdBusy] = useState(false);

  // Legal-hold form.
  const [lhOpen, setLhOpen] = useState(false);
  const [lhTypeId, setLhTypeId] = useState('');
  const [lhResId, setLhResId] = useState('');
  const [lhReason, setLhReason] = useState('');
  const [lhCase, setLhCase] = useState('');
  const [lhBusy, setLhBusy] = useState(false);
  const [lhError, setLhError] = useState<string | null>(null);

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
    Promise.all([
      api.admin.lifecycle.recycleBin.list({ limit: 50 }, tid),
      api.admin.lifecycle.legalHolds.list(tid),
    ])
      .then(([page, h]) => {
        setItems(page.items ?? []);
        setNextCursor(page.next_cursor ?? null);
        setHolds(h);
      })
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tid]);

  useEffect(() => { load(); }, [load, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const handleRestore = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: '恢复资源', message: '确定从回收站恢复该资源？', confirmText: '恢复' }))) return;
    try {
      await api.admin.lifecycle.recycleBin.restore(id, tid);
      toast.success('已恢复');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '恢复失败');
    }
  };

  const handleHardDelete = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({
      title: '彻底删除',
      message: '彻底删除后不可恢复，确定继续？',
      tone: 'danger',
      confirmText: '彻底删除',
    }))) return;
    try {
      await api.admin.lifecycle.recycleBin.hardDelete(id, { destroyed_by: actorId }, tid);
      toast.success('已彻底删除');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
    }
  };

  const handleSweep = async () => {
    if (!(await confirm({ title: '清扫过期', message: '将彻底销毁所有已过期的回收站项，确定执行？', tone: 'danger', confirmText: '清扫' }))) return;
    try {
      const res = await api.admin.lifecycle.sweep(tid);
      const n = typeof res?.destroyed === 'number' ? res.destroyed : 0;
      toast.success(`已清扫 ${n} 项`);
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '清扫失败');
    }
  };

  const handleSoftDelete = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!sdTypeId.trim() || !sdResId.trim()) { setError('请填写资源类型 ID 与资源 ID'); return; }
    setSdBusy(true);
    setError(null);
    try {
      await api.admin.lifecycle.recycleBin.softDelete(
        { resource_type_id: sdTypeId.trim(), resource_id: sdResId.trim(), resource_name: sdName.trim() || null },
        tid,
      );
      toast.success('已软删除');
      setSdTypeId(''); setSdResId(''); setSdName(''); setSdOpen(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '软删除失败');
    } finally {
      setSdBusy(false);
    }
  };

  const handleCreateHold = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!lhTypeId.trim() || !lhResId.trim()) { setLhError('请填写资源类型 ID 与资源 ID'); return; }
    if (lhReason.trim().length < 10) { setLhError('保留理由至少需 10 个字符'); return; }
    setLhBusy(true);
    setLhError(null);
    try {
      await api.admin.lifecycle.legalHolds.create(
        {
          resource_type_id: lhTypeId.trim(),
          resource_id: lhResId.trim(),
          reason: lhReason.trim(),
          case_reference: lhCase.trim() || null,
        },
        tid,
      );
      toast.success('已设置法务保留');
      setLhTypeId(''); setLhResId(''); setLhReason(''); setLhCase(''); setLhOpen(false);
      refresh();
    } catch (err) {
      setLhError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setLhBusy(false);
    }
  };

  const handleReleaseHold = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: '解除法务保留', message: '确定解除该法务保留？', tone: 'danger', confirmText: '解除' }))) return;
    try {
      await api.admin.lifecycle.legalHolds.release(id, { released_by: actorId }, tid);
      toast.success('已解除');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '解除失败');
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">数据生命周期</h1>
        <p className="text-[12px] text-dim mt-0.5">回收站、过期清扫与法务保留（→ lifecycle）</p>
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
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：数据生命周期端点不可达，接线后将自动展示。</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {/* Recycle bin */}
      {!todo && (
        <section className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">回收站</div>
            <div className="flex items-center gap-2">
              {!sdOpen && <button onClick={() => setSdOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 软删除</button>}
              <button onClick={handleSweep} className="px-2.5 py-1 text-[11px] font-mono border border-red-200 text-red-600 rounded-sm hover:bg-red-50">清扫过期</button>
            </div>
          </div>

          {sdOpen && (
            <Dialog title="手工软删除" onClose={() => setSdOpen(false)} dismissable={!sdBusy} className="w-full max-w-2xl">
            <form onSubmit={handleSoftDelete} className="flex flex-col gap-2">
              <div className="text-[11px] text-dim">资源类型 ID 须为已注册类型（如 <code className="font-mono">rt_…</code>），否则下游返回「资源类型未注册」。</div>
              <div className="grid grid-cols-3 gap-2">
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">资源类型 ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={sdTypeId} onChange={(e) => setSdTypeId(e.target.value)} placeholder="rt_document00000000000000001" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">资源 ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={sdResId} onChange={(e) => setSdResId(e.target.value)} placeholder="doc-123" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">资源名称 (可选)</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={sdName} onChange={(e) => setSdName(e.target.value)} />
                </div>
              </div>
              <div className="flex gap-2">
                <button type="submit" disabled={sdBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{sdBusy ? '提交中…' : '软删除'}</button>
                <button type="button" onClick={() => setSdOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
              </div>
            </form>
            </Dialog>
          )}

          {!loading && !todo && items.length === 0 && <div className="font-mono text-[12px] text-dim italic">回收站为空</div>}
          {items.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源类型</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源 ID</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">删除时间</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">过期时间</th>
                    <th className="px-3 py-2 w-40"></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it, i) => (
                    <tr key={it.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                      <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{it.resource_type ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">{it.resource_id ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(it.deleted_at)}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(it.expires_at)}</td>
                      <td className="px-3 py-2 flex items-center gap-2">
                        <button onClick={() => handleRestore(it.id)} className="text-[11px] font-mono text-green-700 border border-green-200 rounded-sm px-2 py-0.5 hover:bg-green-50">恢复</button>
                        <button onClick={() => handleHardDelete(it.id)} className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50">彻底删除</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {nextCursor && <div className="font-mono text-[10.5px] text-dim">还有更多（cursor: {nextCursor}）</div>}
        </section>
      )}

      {/* Legal holds */}
      {!todo && (
        <section className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">法务保留</div>
            {!lhOpen && <button onClick={() => setLhOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建保留</button>}
          </div>

          {lhOpen && (
            <Dialog title="新建法务保留" onClose={() => setLhOpen(false)} dismissable={!lhBusy} className="w-full max-w-2xl">
            <form onSubmit={handleCreateHold} className="flex flex-col gap-2">
              {lhError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{lhError}</div>}
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">资源类型 ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={lhTypeId} onChange={(e) => setLhTypeId(e.target.value)} placeholder="rt_document00000000000000001" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">资源 ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={lhResId} onChange={(e) => setLhResId(e.target.value)} placeholder="doc-123" />
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">保留理由 (≥10 字符)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={lhReason} onChange={(e) => setLhReason(e.target.value)} placeholder="涉诉数据保全" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">案件编号 (可选)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={lhCase} onChange={(e) => setLhCase(e.target.value)} />
              </div>
              <div className="flex gap-2">
                <button type="submit" disabled={lhBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{lhBusy ? '创建中…' : '创建'}</button>
                <button type="button" onClick={() => setLhOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
              </div>
            </form>
            </Dialog>
          )}

          {!loading && !todo && holds.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无法务保留</div>}
          {holds.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源类型</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源 ID</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">理由</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">设置时间</th>
                    <th className="px-3 py-2 w-20"></th>
                  </tr>
                </thead>
                <tbody>
                  {holds.map((h, i) => (
                    <tr key={h.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                      <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{h.resource_type ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">{h.resource_id ?? '—'}</td>
                      <td className="px-3 py-2 text-[12px] text-ink-2 truncate max-w-[220px]">{h.reason ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(h.placed_at)}</td>
                      <td className="px-3 py-2">
                        <button onClick={() => handleReleaseHold(h.id)} className="text-[11px] font-mono text-accent hover:opacity-80">解除</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
