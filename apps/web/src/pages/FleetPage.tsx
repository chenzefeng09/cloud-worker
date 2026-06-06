import { useEffect, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { StatusDot } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
import { api, ApiError } from '../api/client';
import type {
  PoolSnapshot,
  SandboxItem,
  TokenUsage,
  TopologyGraph,
  TopologyNodeWithPos,
  TopologyEdge,
} from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';

// ── Layout algorithm ──────────────────────────────────────────────────────────
// Three-column layout: mcp | worker | task
// Each column is vertically centred and nodes evenly distributed.
// Canvas logical size: 800 × 460

const CANVAS_W = 800;
const CANVAS_H = 460;

/** Attach x/y to every node using a three-column layered layout.
 *  mcp nodes → left column (x≈130)
 *  worker nodes → centre column (x≈400)
 *  task nodes → right column (x≈670)
 */
export function computeLayout(graph: TopologyGraph): {
  nodesWithPos: TopologyNodeWithPos[];
  edges: TopologyEdge[];
} {
  const mcps    = graph.nodes.filter(n => n.type === 'mcp');
  const workers = graph.nodes.filter(n => n.type === 'worker');
  const tasks   = graph.nodes.filter(n => n.type === 'task');

  const colX = { mcp: 130, worker: 400, task: 670 };
  const PAD  = 60; // vertical padding top/bottom

  function spreadY(items: { id: string }[]): Map<string, number> {
    const result = new Map<string, number>();
    const count = items.length;
    if (count === 0) return result;
    const step = count === 1
      ? 0
      : (CANVAS_H - PAD * 2) / (count - 1);
    items.forEach((item, i) => {
      const y = count === 1
        ? CANVAS_H / 2
        : PAD + i * step;
      result.set(item.id, y);
    });
    return result;
  }

  const mcpY    = spreadY(mcps);
  const workerY = spreadY(workers);
  const taskY   = spreadY(tasks);

  const nodesWithPos: TopologyNodeWithPos[] = [
    ...mcps.map(n    => ({ ...n, x: colX.mcp,    y: mcpY.get(n.id)    ?? CANVAS_H / 2 })),
    ...workers.map(n => ({ ...n, x: colX.worker, y: workerY.get(n.id) ?? CANVAS_H / 2 })),
    ...tasks.map(n   => ({ ...n, x: colX.task,   y: taskY.get(n.id)   ?? CANVAS_H / 2 })),
  ];

  return { nodesWithPos, edges: graph.edges };
}

// ── Status → stroke colour ────────────────────────────────────────────────────

function workerStroke(status: string): string {
  switch (status.toLowerCase()) {
    case 'running':  return '#d23a1f';   // accent — active
    case 'starting': return '#c68a17';   // warn
    case 'paused':   return '#75756f';   // dim
    case 'stopped':
    case 'exited':   return '#2c8f5d';   // done/ok
    default:         return '#d8d4c6';   // idle/line
  }
}

function mcpStatusColor(status: string): string {
  return status === 'enabled' ? '#75756f' : '#d23a1f';
}

// ── Topology canvas (real data) ───────────────────────────────────────────────

interface TopologyCanvasProps {
  graph: TopologyGraph;
}

function TopologyCanvas({ graph }: TopologyCanvasProps) {
  const { nodesWithPos, edges } = computeLayout(graph);

  const nodeById = Object.fromEntries(nodesWithPos.map(n => [n.id, n]));

  const workers = nodesWithPos.filter(n => n.type === 'worker');
  const tasks   = nodesWithPos.filter(n => n.type === 'task');
  const mcps    = nodesWithPos.filter(n => n.type === 'mcp');

  if (workers.length === 0 && tasks.length === 0 && mcps.length === 0) {
    return (
      <div
        className="flex-1 min-h-0"
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#f7f5ee',
        }}
      >
        <span className="cw-label" style={{ fontStyle: 'italic' }}>暂无活跃拓扑</span>
      </div>
    );
  }

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
        viewBox={`0 0 ${CANVAS_W} ${CANVAS_H}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}
      >
        {edges.map((edge, i) => {
          const a = nodeById[edge.from];
          const b = nodeById[edge.to];
          if (!a || !b) return null;
          const live = a.type === 'worker' && a.status.toLowerCase() === 'running' && edge.kind === 'worker→task';
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
            left: `calc(${(t.x / CANVAS_W) * 100}% - 75px)`,
            top:  `calc(${(t.y / CANVAS_H) * 100}% - 18px)`,
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
            left: `calc(${(m.x / CANVAS_W) * 100}% - 50px)`,
            top:  `calc(${(m.y / CANVAS_H) * 100}% - 14px)`,
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
          <span style={{ color: mcpStatusColor(m.status) }}>⊞</span>
          <span style={{ overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>{m.label}</span>
        </div>
      ))}

      {/* Worker nodes */}
      {workers.map(w => {
        const active = w.status.toLowerCase() === 'running';
        const stroke = workerStroke(w.status);
        const glyph  = w.label.slice(0, 1).toUpperCase();
        return (
          <div
            key={w.id}
            style={{
              position: 'absolute',
              left: `calc(${(w.x / CANVAS_W) * 100}% - 32px)`,
              top:  `calc(${(w.y / CANVAS_H) * 100}% - 32px)`,
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
              {glyph}
            </span>
            <span style={{
              fontFamily: "'JetBrains Mono', ui-monospace, monospace",
              fontSize: 10,
              color: '#75756f',
              overflow: 'hidden',
              whiteSpace: 'nowrap',
              maxWidth: 54,
              textOverflow: 'ellipsis',
            }}>
              {w.label.slice(0, 8)}
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
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [topology, setTopology]   = useState<TopologyGraph | null>(null);
  const [topoLoading, setTopoLoading] = useState(false);

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

  // Fetch topology graph separately — best-effort
  const fetchTopology = () => {
    setTopoLoading(true);
    let cancelled = false;
    api.fleet.topology()
      .then(data => { if (!cancelled) setTopology(data); })
      .catch(() => { /* best-effort — canvas shows empty state */ })
      .finally(() => { if (!cancelled) setTopoLoading(false); });
    return () => { cancelled = true; };
  };

  useEffect(fetchAll, []);
  useEffect(fetchTopology, []);

  // Fetch real token usage totals for the sidebar token block.
  useEffect(() => {
    let cancelled = false;
    api.runs.tokenUsage().then((data) => {
      if (!cancelled) setTokenUsage(data);
    }).catch(() => { /* best-effort */ });
    return () => { cancelled = true; };
  }, []);

  const totalSandboxes = pool?.total ?? 0;
  const idleTotal = pool
    ? Object.values(pool.by_template).reduce((sum, t) => sum + t.idle, 0)
    : 0;
  const busyTotal = totalSandboxes - idleTotal;

  // Refresh both rail + topology
  const handleRefresh = () => {
    fetchAll();
    fetchTopology();
  };

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: '首页', to: '/home' }, { label: '工作节点' }]}
        actions={
          <button
            className="cw-btn cw-btn-sm"
            onClick={handleRefresh}
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
        {/* LEFT — topology canvas (real data) */}
        <div className="flex flex-col min-h-0">
          {topoLoading ? (
            <div
              className="flex-1 min-h-0"
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: '#f7f5ee',
              }}
            >
              <span className="cw-label" style={{ fontStyle: 'italic' }}>加载拓扑中…</span>
            </div>
          ) : topology ? (
            <TopologyCanvas graph={topology} />
          ) : (
            <div
              className="flex-1 min-h-0"
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                background: '#f7f5ee',
              }}
            >
              <span className="cw-label" style={{ fontStyle: 'italic' }}>暂无活跃拓扑</span>
            </div>
          )}
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

          {/* Token usage block — real data from /runs/token-usage */}
          <div style={{ padding: 14, borderTop: '1px solid #d8d4c6' }}>
            <div className="cw-label" style={{ marginBottom: 6 }}>累计 Token</div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <span
                style={{
                  fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                  fontSize: 22,
                  fontWeight: 600,
                }}
              >
                {tokenUsage !== null
                  ? tokenUsage.total_tokens.toLocaleString()
                  : '…'}
              </span>
              <span className="cw-label" style={{ letterSpacing: 0 }}>tokens</span>
            </div>
            {tokenUsage !== null && (
              <div style={{
                fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                fontSize: 10,
                color: 'var(--dim)',
                marginTop: 4,
              }}>
                in {tokenUsage.input_tokens.toLocaleString()} · out {tokenUsage.output_tokens.toLocaleString()}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
