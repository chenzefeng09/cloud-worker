import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type { FeatureFlag, FeatureFlagUpdate, KillSwitchRequest } from '../../api/types';
import { useConfirm, usePrompt } from '../components/dialogs';

/** Summarise a flag's rollout strategy from its surfaced fields. */
function strategySummary(flag: FeatureFlag): string {
  const parts: string[] = [];
  if (flag.default_value !== undefined) parts.push(`默认 ${flag.default_value ? '开' : '关'}`);
  if (flag.fail_open !== undefined) parts.push(flag.fail_open ? 'fail-open' : 'fail-close');
  if (flag.owner_team) parts.push(`@${flag.owner_team}`);
  return parts.length > 0 ? parts.join(' · ') : '—';
}

export function FeatureFlagsPage() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const confirm = useConfirm();
  const prompt = usePrompt();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    try {
      const items = await api.admin.flags.list();
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
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleToggle = async (flag: FeatureFlag) => {
    setBusyKey(flag.key);
    setError(null);
    // Preserve the flag's other fields so the upsert does not wipe them.
    const body: FeatureFlagUpdate = {
      enabled: !flag.enabled,
      name: flag.name,
      description: flag.description,
      default_value: flag.default_value,
      fail_open: flag.fail_open,
      owner_team: flag.owner_team,
      tags: flag.tags,
    };
    try {
      await api.admin.flags.put(flag.key, body);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setBusyKey(null);
    }
  };

  const handleKillSwitch = async (flag: FeatureFlag) => {
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
      await api.admin.flags.killSwitch(flag.key, body);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Feature Flags</h1>
        <p className="text-[12px] text-dim mt-0.5">
          底座 Feature Flag 服务（Unleash 封装）的灰度开关与紧急熔断
        </p>
      </div>

      <div className="border border-line rounded-md px-4 py-2.5 bg-paper-2 text-[12px] text-dim">
        本页对接底座 feature-flag 服务（Unleash 封装）。开关切换会写回上游策略；紧急 Kill 开关将强制关闭对应 flag，覆盖所有灰度策略，请谨慎使用。
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
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
                      {flag.description || <span className="text-dim">—</span>}
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
    </div>
  );
}
