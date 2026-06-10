import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useConfirm } from '../components/dialogs';
import type {
  DailyTokenUsage,
  OpsRun,
  PoolSnapshot,
  SandboxInfo,
} from '../../api/types';

// ─── Token usage bar chart (CSS/SVG, no chart lib) ────────────────────────────

function UsageChartBlock() {
  const [days, setDays] = useState<DailyTokenUsage[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    api.admin.ops
      .usageDaily({ days: 14 })
      .then((data) => { if (!cancelled) setDays(data.days); })
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

  const max = days && days.length > 0 ? Math.max(...days.map((d) => d.tokens), 1) : 1;

  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">Token 用量趋势（近 14 天）</div>
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {isTodoEndpoint && (
        <div className="text-[12px] text-dim italic">该能力暂未启用：用量统计端点尚未就绪。</div>
      )}
      {days && days.length === 0 && !isTodoEndpoint && (
        <div className="font-mono text-[12px] text-dim italic">暂无用量数据</div>
      )}
      {days && days.length > 0 && (
        <div className="flex items-end gap-1.5 h-40 pt-2">
          {days.map((d) => {
            const pct = Math.round((d.tokens / max) * 100);
            return (
              <div key={d.day} className="flex-1 flex flex-col items-center justify-end gap-1 group h-full">
                <span className="text-[9px] font-mono text-dim opacity-0 group-hover:opacity-100 transition-opacity">
                  {d.tokens.toLocaleString()}
                </span>
                <div
                  className="w-full bg-accent/70 group-hover:bg-accent rounded-sm transition-colors"
                  style={{ height: `${Math.max(pct, 2)}%` }}
                  title={`${d.day}: ${d.tokens.toLocaleString()} tokens`}
                />
                <span className="text-[9px] font-mono text-dim">{d.day.slice(5)}</span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ─── Sandbox pool + live sandbox list ─────────────────────────────────────────

function sandboxKey(s: SandboxInfo): string {
  return s.container_id ?? s.id ?? '';
}

function SandboxBlock() {
  const [pool, setPool] = useState<PoolSnapshot | null>(null);
  const [sandboxes, setSandboxes] = useState<SandboxInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const confirm = useConfirm();

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    Promise.all([api.admin.ops.pool(), api.admin.ops.sandboxes()])
      .then(([p, s]) => {
        if (cancelled) return;
        setPool(p);
        setSandboxes(s);
      })
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

  const stop = async (s: SandboxInfo) => {
    const id = sandboxKey(s);
    if (!id) return;
    if (!(await confirm({ title: '停止沙箱', message: `确认停止沙箱「${s.name ?? id}」？`, tone: 'danger', confirmText: '停止' }))) return;
    try {
      await api.admin.ops.stopSandbox(id);
      setSandboxes((prev) => prev.filter((x) => sandboxKey(x) !== id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '停止失败');
    }
  };

  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">沙箱池</div>
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {isTodoEndpoint && (
        <div className="text-[12px] text-dim italic">该能力暂未启用：沙箱池端点尚未就绪。</div>
      )}

      {pool && (
        <div className="flex flex-wrap gap-3">
          <div className="border border-line-2 rounded-md px-3 py-2 min-w-[100px]">
            <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">总数</div>
            <div className="text-[18px] font-display text-ink">{pool.total}</div>
          </div>
          {Object.entries(pool.by_template ?? {}).map(([tpl, info]) => (
            <div key={tpl} className="border border-line-2 rounded-md px-3 py-2 min-w-[100px]">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim truncate max-w-[140px]">{tpl}</div>
              <div className="text-[13px] font-mono text-ink-2">
                空闲 {info.idle} / 共 {info.total}
              </div>
            </div>
          ))}
        </div>
      )}

      {!isTodoEndpoint && !loading && sandboxes.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">暂无运行中的沙箱</div>
      )}

      {sandboxes.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">容器 ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">镜像</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">租户</th>
                <th className="px-3 py-2 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {sandboxes.map((s) => {
                const id = sandboxKey(s);
                return (
                  <tr key={id || s.name} className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                    <td className="px-3 py-2 font-mono text-[12px] text-ink-2 truncate max-w-[160px]">
                      {id ? `${id.slice(0, 12)}…` : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[180px]">{s.image || '—'}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.status}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]">{s.tenant_id}</td>
                    <td className="px-3 py-2">
                      <button
                        onClick={() => stop(s)}
                        className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-accent hover:opacity-80 transition-opacity"
                      >
                        停止
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ─── Run list (scheduler list endpoint not yet available → 404 fallback) ──────

const RUN_STATUS_COLOR: Record<string, string> = {
  PENDING: 'text-dim',
  RUNNING: 'text-warn',
  COMPLETED: 'text-ok',
  FAILED: 'text-accent',
  CANCELLED: 'text-dim',
};

function RunsBlock() {
  const [runs, setRuns] = useState<OpsRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    // scheduler 下游暂无列表端点，会 404/405；保留调用以便下游补齐后自动生效
    api.admin.ops
      .runs()
      .then((data) => { if (!cancelled) setRuns(data); })
      .catch((err) => {
        if (cancelled) return;
        // scheduler 暂无列表端点：可能返回 404 / 405 (Method Not Allowed) / 501 / 502。
        if (
          err instanceof ApiError &&
          (err.status === 404 || err.status === 405 || err.status === 501 || err.status === 502)
        ) {
          setIsTodoEndpoint(true);
        } else {
          setError(err instanceof ApiError ? err.message : '加载失败');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">运行列表</div>
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {isTodoEndpoint && (
        <div className="text-[12px] text-dim italic">运行列表能力待下游支持：scheduler 暂未提供列表端点，接线后将自动展示。</div>
      )}
      {!isTodoEndpoint && !loading && runs.length === 0 && !error && (
        <div className="font-mono text-[12px] text-dim italic">暂无运行记录</div>
      )}
      {runs.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">运行 ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">任务</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Token</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">创建时间</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 font-mono text-[12px] text-ink-2 truncate max-w-[140px]">
                    {r.id.slice(0, 12)}…
                  </td>
                  <td className={`px-3 py-2 font-mono text-[11px] ${RUN_STATUS_COLOR[r.status] ?? 'text-dim'}`}>
                    {r.status}
                  </td>
                  <td className="px-3 py-2 text-[12px] text-dim truncate max-w-[240px]">
                    {r.task_description || '—'}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                    {r.total_tokens != null ? r.total_tokens.toLocaleString() : '—'}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {new Date(r.created_at).toLocaleString('zh-CN')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function OpsDashboardPage() {
  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">运行与沙箱</h1>
        <p className="text-[12px] text-dim mt-0.5">Token 用量、沙箱池与运行监控</p>
      </div>

      <UsageChartBlock />
      <SandboxBlock />
      <RunsBlock />
    </div>
  );
}
