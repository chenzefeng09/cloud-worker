import { useEffect, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { StatusDot } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
import { api, ApiError } from '../api/client';
import type { PoolSnapshot, SandboxItem } from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';
import {
  FLEET_TOPOLOGY,
  type TopologyWorkerNode,
} from '../lib/designFixtures';

// ── Topology helpers ──────────────────────────────────────────────

function statusStroke(s: TopologyWorkerNode['status']): string {
  switch (s) {
    case 'thinking': return '#d23a1f';   // accent
    case 'awaiting': return '#c68a17';   // warn
    case 'paused':   return '#75756f';   // dim
    case 'done':     return '#2c8f5d';   // ok
    default:         return '#d8d4c6';   // line (idle)
  }
}

// ── Static topology canvas (DESIGN FIXTURE — remove when backend provides topology) ──

function TopologyCanvas() {
  const { workers, tasks, mcps, links } = FLEET_TOPOLOGY;

  // Build lookup maps by id
  const workerById = Object.fromEntries(workers.map(w => [w.id, w]));
  const taskById   = Object.fromEntries(tasks.map(t  => [t.id, t]));
  const mcpById    = Object.fromEntries(mcps.map(m   => [m.id, m]));

  return (
    <div style={{ position: 'relative', overflow: 'hidden', background: '#f7f5ee' }} className="flex-1 min-h-0">
      {/* dot-grid background */}
      <div
        aria-hidden
        style={{
          position: 'absolute', inset: 0,
          backgroundImage:
            'linear-gradient(#d8d4c6 1px, transparent 1px), linear-gradient(90deg, #d8d4c6 1px, transparent 1px)',
          backgroundSize: '32px 32px',
          opacity: 0.35,
          pointerEvents: 'none',
        }}
      />

      {/* SVG edges + animated motion dots */}
      <svg
        viewBox="0 0 800 460"
        preserveAspectRatio="xMidYMid meet"
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}
      >
        {links.map((link, i) => {
          const a = workerById[link.fromWorker];
          const b = link.toTask ? taskById[link.toTask] : link.toMcp ? mcpById[link.toMcp] : null;
          if (!a || !b) return null;
          const live = !!link.live;
          const pathD = `M${a.x},${a.y} L${b.x},${b.y}`;
          return (
            <g key={i}>
              <line
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={live ? '#d23a1f' : '#d8d4c6'}
                strokeWidth={live ? 1.5 : 1}
                strokeDasharray={live ? '0' : '3,4'}
                opacity={live ? 0.85 : 0.55}
              />
              {live && (
                <circle r="3" fill="#d23a1f">
                  <animateMotion dur="2.4s" repeatCount="indefinite" path={pathD} />
                </circle>
              )}
            </g>
          );
        })}
      </svg>

      {/* Task nodes */}
      {tasks.map(t => (
        <div
          key={t.id}
          style={{
            position: 'absolute',
            left: `calc(${(t.x / 800) * 100}% - 75px)`,
            top:  `calc(${(t.y / 460) * 100}% - 18px)`,
            width: 150,
            background: '#fffdf7',
            border: '1px dashed #d8d4c6',
            borderRadius: 4,
            padding: '5px 8px',
            fontFamily: "'JetBrains Mono', ui-monospace, monospace",
            fontSize: 11,
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            zIndex: 1,
          }}
        >
          <span style={{ color: '#75756f' }}>▤</span>
          <span style={{ flex: 1, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
            {t.label}
          </span>
        </div>
      ))}

      {/* MCP pill nodes */}
      {mcps.map(m => (
        <div
          key={m.id}
          style={{
            position: 'absolute',
            left: `calc(${(m.x / 800) * 100}% - 50px)`,
            top:  `calc(${(m.y / 460) * 100}% - 14px)`,
            width: 100,
            background: '#f0ede3',
            border: '1px solid #d8d4c6',
            borderRadius: 999,
            padding: '3px 8px',
            fontFamily: "'JetBrains Mono', ui-monospace, monospace",
            fontSize: 10.5,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 5,
            zIndex: 1,
          }}
        >
          <span style={{ color: m.mcpStatus === 'auth needed' ? '#d23a1f' : '#75756f' }}>⊞</span>
          <span>{m.label}</span>
        </div>
      ))}

      {/* Worker nodes */}
      {workers.map(w => {
        const active = w.status === 'thinking';
        const stroke = statusStroke(w.status);
        return (
          <div
            key={w.id}
            style={{
              position: 'absolute',
              left: `calc(${(w.x / 800) * 100}% - 32px)`,
              top:  `calc(${(w.y / 460) * 100}% - 32px)`,
              width: 64,
              height: 64,
              borderRadius: '50%',
              background: '#fffdf7',
              border: `1.5px solid ${stroke}`,
              boxShadow: active ? '0 0 0 6px rgba(210,58,31,0.10)' : 'none',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 2,
            }}
          >
            <span style={{
              fontFamily: "'JetBrains Mono', ui-monospace, monospace",
              fontSize: 18,
              fontWeight: 600,
              color: active ? '#d23a1f' : '#3c3d40',
              letterSpacing: '-0.05em',
            }}>
              {w.glyph}
            </span>
            <span style={{
              fontFamily: "'JetBrains Mono', ui-monospace, monospace",
              fontSize: 10,
              color: '#75756f',
            }}>
              {w.id}
            </span>
            {/* status blip */}
            <span
              className={active ? 'cw-dot animate-blip' : 'cw-dot'}
              style={{
                position: 'absolute',
                bottom: 3,
                right: 3,
                background: stroke,
                border: '2px solid #fffdf7',
              }}
            />
          </div>
        );
      })}

      {/* Legend */}
      <div
        style={{
          position: 'absolute',
          left: 16,
          bottom: 16,
          background: '#fffdf7',
          border: '1px solid #d8d4c6',
          borderRadius: 4,
          padding: 10,
          display: 'flex',
          flexDirection: 'column',
          gap: 5,
          fontFamily: "'JetBrains Mono', ui-monospace, monospace",
          fontSize: 10.5,
          zIndex: 3,
        }}
      >
        <div className="cw-label" style={{ marginBottom: 2 }}>图例</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-block', width: 22, height: 1.5, background: '#d23a1f' }} />
          <span>活跃链接</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-block', width: 22, height: 0, borderTop: '1px dashed #d8d4c6' }} />
          <span>空闲链接</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-block', width: 12, height: 12, borderRadius: '50%', border: '1px solid #3c3d40' }} />
          <span>节点</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-block', width: 16, height: 9, border: '1px dashed #3c3d40', borderRadius: 2 }} />
          <span>任务</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ display: 'inline-block', width: 16, height: 9, border: '1px solid #3c3d40', borderRadius: 99 }} />
          <span>工具</span>
        </div>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────

export function FleetPage() {
  const [pool, setPool]           = useState<PoolSnapshot | null>(null);
  const [sandboxes, setSandboxes] = useState<SandboxItem[]>([]);
  const [loading, setLoading]     = useState(false);
  const [error, setError]         = useState<string | null>(null);

  const fetchAll = () => {
    setLoading(true);
    setError(null);

    let cancelled = false;
    const run = async () => {
      try {
        const [poolRes, sbRes] = await Promise.all([
          api.fleet.poolSnapshot(),
          api.fleet.listSandboxes(),
        ]);
        if (cancelled) return;
        setPool(poolRes);
        setSandboxes(sbRes.items);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : '加载工作节点数据失败，请稍后重试。');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    run();
    return () => { cancelled = true; };
  };

  useEffect(fetchAll, []);

  const totalSandboxes = pool?.total ?? 0;
  const idleTotal = pool
    ? Object.values(pool.by_template).reduce((sum, t) => sum + t.idle, 0)
    : 0;
  const busyTotal = totalSandboxes - idleTotal;

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: '首页', to: '/home' }, { label: '工作节点' }]}
        actions={
          <button
            className="cw-btn cw-btn-sm"
            onClick={fetchAll}
          >
            刷新
          </button>
        }
      />

      {/* two-column body */}
      <div
        className="flex-1 min-h-0"
        style={{ display: 'grid', gridTemplateColumns: '1fr 280px' }}
      >
        {/* LEFT — static topology canvas (DESIGN FIXTURE) */}
        <div className="flex flex-col min-h-0">
          <TopologyCanvas />
        </div>

        {/* RIGHT — real fleet data rail */}
        <aside
          className="flex flex-col min-h-0"
          style={{ borderLeft: '1px solid #d8d4c6', background: '#f7f5ee' }}
        >
          {/* header: pool stats */}
          <div style={{ padding: 16, borderBottom: '1px solid #e8e4d8' }}>
            <div className="cw-label" style={{ marginBottom: 8 }}>工作节点</div>

            {error && (
              <div className="font-mono text-[11px]" style={{ color: '#d23a1f', marginBottom: 8 }}>
                {error}
              </div>
            )}

            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <span
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  fontSize: 28,
                  fontWeight: 600,
                  letterSpacing: '-0.02em',
                }}
              >
                {loading ? '…' : totalSandboxes}
              </span>
              <span className="cw-label" style={{ fontSize: 12, letterSpacing: 0 }}>沙箱</span>
            </div>

            {!loading && (
              <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
                {busyTotal > 0 && <Chip accent>● {busyTotal} 运行中</Chip>}
                {idleTotal > 0 && <Chip dashed>{idleTotal} 空闲</Chip>}
                {totalSandboxes === 0 && <Chip dashed>无活跃沙箱</Chip>}
              </div>
            )}
          </div>

          {/* template pool breakdown */}
          {!loading && pool && Object.keys(pool.by_template).length > 0 && (
            <div style={{ padding: '12px 16px', borderBottom: '1px solid #e8e4d8' }}>
              <div className="cw-label" style={{ marginBottom: 8 }}>模板池</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {Object.entries(pool.by_template).map(([tmpl, info]) => (
                  <div
                    key={tmpl}
                    style={{
                      display: 'grid',
                      gridTemplateColumns: '1fr auto auto',
                      gap: 8,
                      alignItems: 'center',
                      padding: '6px 10px',
                      border: '1px solid #d8d4c6',
                      borderRadius: 4,
                      background: '#fffdf7',
                    }}
                  >
                    <span
                      style={{
                        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                        fontSize: 11,
                        fontWeight: 600,
                        overflow: 'hidden',
                        whiteSpace: 'nowrap',
                        textOverflow: 'ellipsis',
                      }}
                    >
                      {tmpl}
                    </span>
                    <Chip>{info.idle} 空闲</Chip>
                    <span className="cw-label" style={{ fontSize: 10 }}>{info.total}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* sandbox list */}
          <div className="cw-scroll flex-1 min-h-0">
            {loading && (
              <div
                style={{ padding: '12px 16px' }}
                className="cw-label"
              >
                加载中…
              </div>
            )}

            {!loading && sandboxes.length > 0 && (
              <>
                <div style={{ padding: '10px 16px 4px' }}>
                  <div className="cw-label">运行中的节点 · {sandboxes.length}</div>
                </div>
                {sandboxes.map((sb, idx) => {
                  const sbId = sb.container_id || sb.id || '';
                  const isRunning = sb.status?.toLowerCase() === 'running';
                  return (
                    <div
                      key={sbId || idx}
                      style={{
                        padding: '10px 16px',
                        borderBottom: '1px solid #e8e4d8',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 10,
                      }}
                    >
                      {/* avatar glyph */}
                      <span className={`cw-avatar${isRunning ? ' accent' : ''}`}>
                        {(sb.name || sbId).slice(0, 1).toUpperCase()}
                      </span>

                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span
                            style={{
                              fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                              fontSize: 11.5,
                              fontWeight: 600,
                              overflow: 'hidden',
                              whiteSpace: 'nowrap',
                              textOverflow: 'ellipsis',
                            }}
                          >
                            {sb.name || (sbId ? sbId.slice(0, 12) : '—')}
                          </span>
                          <StatusDot status={isRunning ? 'thinking' : 'idle'} label={sb.status} />
                        </div>
                        {sb.session_id && (
                          <div
                            className="cw-label"
                            style={{ marginTop: 2, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}
                          >
                            会话 · {sb.session_id.slice(0, 12)}
                          </div>
                        )}
                        {sb.created_at && (
                          <div className="cw-label" style={{ marginTop: 2 }}>
                            {formatRelativeTimeZh(sb.created_at)}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </>
            )}

            {!loading && sandboxes.length === 0 && !error && (
              <div style={{ padding: '12px 16px' }}>
                <span
                  className="cw-label"
                  style={{ fontStyle: 'italic' }}
                >
                  暂无运行中的工作节点
                </span>
              </div>
            )}
          </div>

          {/* cost block silhouette (DESIGN FIXTURE — replace when backend provides spend data) */}
          <div style={{ padding: 14, borderTop: '1px solid #d8d4c6' }}>
            <div className="cw-label" style={{ marginBottom: 6 }}>今日费用</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <span
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  fontSize: 22,
                  fontWeight: 600,
                }}
              >
                $4.21
              </span>
              <span className="cw-label" style={{ letterSpacing: 0 }}>/ $50 上限</span>
            </div>
            <div
              style={{
                height: 3,
                background: '#e8e4d8',
                borderRadius: 2,
                overflow: 'hidden',
                marginTop: 6,
              }}
            >
              <div style={{ width: '8.4%', height: '100%', background: '#d23a1f' }} />
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
