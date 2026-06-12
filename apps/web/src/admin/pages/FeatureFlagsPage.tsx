import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type { FeatureFlag, FeatureFlagUpdate, KillSwitchRequest } from '../../api/types';
import { useConfirm, usePrompt } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';

// ─── Chinese description mapping (future i18n: move to locale file) ─────────

const FLAG_DESCRIPTIONS: Record<string, string> = {
  'ai.thinking_ui': '显示 AI 思考/推理过程面板',
  'auth.social_login.github': '启用 GitHub 社交登录',
  'auth.social_login.google': '启用 Google 社交登录',
  'auth.social_login.wechat': '启用微信社交登录',
  'billing.saas_mode': '启用 Stripe 订阅制 SaaS 计费',
  'compliance.gmtls_enabled': '启用国密 TLS（GMTLS）',
  'compliance.sm_algorithms': '启用 SM2/SM3/SM4 国密算法',
  'export.allow_l4_download': '允许 L4 级别数据导出下载',
};

function flagDescription(flag: FeatureFlag): string {
  return FLAG_DESCRIPTIONS[flag.key] || flag.description || '—';
}

/** Summarise a flag's rollout strategy from its surfaced fields. */
function strategySummary(flag: FeatureFlag): string {
  const parts: string[] = [];
  if (flag.default_value !== undefined) parts.push(`默认 ${flag.default_value ? '开' : '关'}`);
  if (flag.fail_open !== undefined) parts.push(flag.fail_open ? 'fail-open' : 'fail-close');
  if (flag.owner_team) parts.push(`@${flag.owner_team}`);
  return parts.length > 0 ? parts.join(' · ') : '—';
}

// ─── Create Flag inline form ────────────────────────────────────────────────

const EMPTY_CREATE = { key: '', name: '', description: '', enabled: false, default_value: false, fail_open: false, user_overridable: false };

function CreateFlagForm({
  tenantId,
  onCreated,
  onCancel,
}: {
  tenantId: string;
  onCreated: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState(EMPTY_CREATE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.key.trim()) { setError('Key 不能为空'); return; }
    setBusy(true);
    setError(null);
    try {
      await api.admin.flags.put(
        form.key.trim(),
        {
          name: form.name || undefined,
          description: form.description || undefined,
          enabled: form.enabled,
          default_value: form.default_value,
          fail_open: form.fail_open,
          user_overridable: form.user_overridable,
        },
        tenantId,
      );
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="新增 Feature Flag" onClose={onCancel} dismissable={!busy} className="w-full max-w-lg">
      <form onSubmit={handleSubmit} className="space-y-3">
        {error && (
          <div className="text-[12px] text-accent">{error}</div>
        )}

        <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 items-center text-[12px]">
          <label className="font-mono text-dim">Key *</label>
          <input
            className="border border-line rounded-sm px-2 py-1 font-mono text-[12px] bg-paper focus:outline-none focus:border-ink-3"
            placeholder="e.g. billing.new_feature"
            value={form.key}
            onChange={(e) => set({ key: e.target.value })}
          />

          <label className="font-mono text-dim">Name</label>
          <input
            className="border border-line rounded-sm px-2 py-1 font-mono text-[12px] bg-paper focus:outline-none focus:border-ink-3"
            placeholder="可选的显示名"
            value={form.name}
            onChange={(e) => set({ name: e.target.value })}
          />

          <label className="font-mono text-dim">描述</label>
          <input
            className="border border-line rounded-sm px-2 py-1 font-mono text-[12px] bg-paper focus:outline-none focus:border-ink-3"
            placeholder="可选"
            value={form.description}
            onChange={(e) => set({ description: e.target.value })}
          />

          <span />
          <div className="flex items-center gap-4 text-[11px] font-mono text-ink-2">
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
              启用
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" checked={form.default_value} onChange={(e) => set({ default_value: e.target.checked })} />
              默认值 开
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" checked={form.fail_open} onChange={(e) => set({ fail_open: e.target.checked })} />
              fail-open
            </label>
            <label className="flex items-center gap-1.5 cursor-pointer">
              <input type="checkbox" checked={form.user_overridable} onChange={(e) => set({ user_overridable: e.target.checked })} />
              允许用户覆盖
            </label>
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="px-3 py-1.5 text-[11px] font-mono rounded-sm border border-line text-dim hover:bg-paper-2 disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-3 py-1.5 text-[11px] font-mono rounded-sm border border-green-200 text-green-700 hover:bg-green-50 disabled:opacity-50"
          >
            创建
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ─── Main page ──────────────────────────────────────────────────────────────

export function FeatureFlagsPage() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const confirm = useConfirm();
  const prompt = usePrompt();

  const {
    tenants, tenantsLoading, setTenants, setTenantsLoading,
    selectedTenantId, setSelectedTenantId,
  } = useAdminStore();

  // Bootstrap tenant list
  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const load = useCallback(async () => {
    if (!selectedTenantId) return;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    try {
      const items = await api.admin.flags.list(selectedTenantId);
      setFlags(items);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
        setIsTodoEndpoint(true);
        setFlags([]);
      } else {
        setError(err instanceof ApiError ? err.message : '加载失败');
      }
    } finally {
      setLoading(false);
    }
  }, [selectedTenantId]);

  useEffect(() => {
    setFlags([]);
    setShowCreate(false);
    load();
  }, [load]);

  const handleToggle = async (flag: FeatureFlag) => {
    if (!selectedTenantId) return;
    setBusyKey(flag.key);
    setError(null);
    const body: FeatureFlagUpdate = {
      enabled: !flag.enabled,
      name: flag.name,
      description: flag.description,
      default_value: flag.default_value,
      fail_open: flag.fail_open,
      owner_team: flag.owner_team,
      tags: flag.tags,
      user_overridable: flag.user_overridable,
    };
    try {
      await api.admin.flags.put(flag.key, body, selectedTenantId);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setBusyKey(null);
    }
  };

  const handleKillSwitch = async (flag: FeatureFlag) => {
    if (!selectedTenantId) return;
    const tripped = flag.kill_switch_active;
    const verb = tripped ? '解除' : '触发';
    if (
      !(await confirm({
        title: 'Kill 开关',
        message:
          `确定要${verb} Feature Flag "${flag.key}" 的紧急 Kill 开关吗？\n` +
          (tripped ? '解除后将恢复正常策略评估。' : '触发后该 flag 将被强制关闭，覆盖所有策略。'),
        tone: 'danger',
        confirmText: verb,
      }))
    )
      return;
    const reason = await prompt({
      title: `${verb} Kill 开关`,
      label: '原因（将记入审计）',
      defaultValue: tripped ? '恢复服务' : '紧急熔断',
      multiline: true,
    });
    if (reason === null) return;
    setBusyKey(flag.key);
    setError(null);
    const body: KillSwitchRequest = { active: !tripped, reason: reason || undefined };
    try {
      await api.admin.flags.killSwitch(flag.key, body, selectedTenantId);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Feature Flags</h1>
          <p className="text-[12px] text-dim mt-0.5">
            底座 Feature Flag 服务（Unleash 封装）的灰度开关与紧急熔断
          </p>
        </div>
        {selectedTenantId && !isTodoEndpoint && (
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="px-3 py-1.5 text-[11px] font-mono rounded-sm border border-line text-ink-2 hover:bg-paper-2"
          >
            + 新增
          </button>
        )}
      </div>

      {/* ─── 租户选择 ─────────────────────────────────────────── */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[{ value: '', label: '— 请选择 —' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {!selectedTenantId && (
        <div className="font-mono text-[12px] text-dim italic">选择一个租户以查看其 Feature Flags</div>
      )}

      {selectedTenantId && (
        <>
          <div className="border border-line rounded-md px-4 py-2.5 bg-paper-2 text-[12px] text-dim">
            本页对接底座 feature-flag 服务（Unleash 封装）。开关切换会写回上游策略；紧急 Kill 开关将强制关闭对应 flag，覆盖所有灰度策略，请谨慎使用。
          </div>

          {error && (
            <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
              {error}
            </div>
          )}

          {showCreate && (
            <CreateFlagForm
              tenantId={selectedTenantId}
              onCreated={() => { setShowCreate(false); load(); }}
              onCancel={() => setShowCreate(false)}
            />
          )}

          {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

          {isTodoEndpoint && (
            <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
              该能力服务未启用（Feature Flag 代理端点不可达）。接线后将自动展示开关列表。
            </div>
          )}

          {!loading && !isTodoEndpoint && flags.length === 0 && (
            <div className="font-mono text-[12px] text-dim italic">暂无 Feature Flag</div>
          )}

          {!loading && !isTodoEndpoint && flags.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Key</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">描述</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">策略</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {flags.map((flag) => {
                    const busy = busyKey === flag.key;
                    return (
                      <tr key={flag.key} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2.5 align-top">
                          <div className="font-mono text-[12px] text-ink">{flag.key}</div>
                          {flag.name && flag.name !== flag.key && (
                            <div className="text-[11px] text-dim mt-0.5">{flag.name}</div>
                          )}
                        </td>
                        <td className="px-3 py-2.5 align-top text-[12px] text-ink-2 max-w-[280px]">
                          {flagDescription(flag)}
                        </td>
                        <td className="px-3 py-2.5 align-top whitespace-nowrap">
                          <span className="inline-flex items-center gap-1.5">
                            <span
                              className={`inline-block w-1.5 h-1.5 rounded-full ${flag.enabled ? 'bg-green-500' : 'bg-gray-300'}`}
                            />
                            <span className="font-mono text-[11px] text-ink-2">{flag.enabled ? '启用' : '停用'}</span>
                          </span>
                          {flag.kill_switch_active && (
                            <span className="ml-2 inline-flex items-center font-mono text-[10px] text-red-700 bg-red-50 border border-red-200 rounded-sm px-1.5 py-0.5">
                              KILL
                            </span>
                          )}
                          {flag.user_overridable && (
                            <span className="ml-2 inline-flex items-center font-mono text-[10px] text-blue-700 bg-blue-50 border border-blue-200 rounded-sm px-1.5 py-0.5">
                              用户可覆盖
                            </span>
                          )}
                        </td>
                        <td className="px-3 py-2.5 align-top font-mono text-[11px] text-dim whitespace-nowrap">
                          {strategySummary(flag)}
                        </td>
                        <td className="px-3 py-2.5 align-top">
                          <div className="flex items-center gap-2 justify-end">
                            <button
                              type="button"
                              disabled={busy || flag.kill_switch_active}
                              onClick={() => handleToggle(flag)}
                              title={flag.kill_switch_active ? 'Kill 开关已触发，请先解除' : undefined}
                              className={[
                                'px-2.5 py-1 text-[11px] font-mono rounded-sm border disabled:opacity-50',
                                flag.enabled
                                  ? 'text-amber-700 border-amber-200 hover:bg-amber-50'
                                  : 'text-green-700 border-green-200 hover:bg-green-50',
                              ].join(' ')}
                            >
                              {flag.enabled ? '关闭' : '开启'}
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => handleKillSwitch(flag)}
                              className={[
                                'px-2.5 py-1 text-[11px] font-mono rounded-sm border disabled:opacity-50',
                                flag.kill_switch_active
                                  ? 'text-green-700 border-green-200 hover:bg-green-50'
                                  : 'text-red-600 border-red-200 hover:bg-red-50',
                              ].join(' ')}
                            >
                              {flag.kill_switch_active ? '解除 Kill' : '紧急 Kill'}
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
