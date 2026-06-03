// TODO · 接入完整的工作区管理（新建工作区流程、拖拽排序、设置面板）
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useWorkspace } from '../lib/workspaceContext';

const FILE_GLYPH: Record<string, string> = {
  md: '◎',
  json: '{}',
  yaml: '≡',
  csv: '⊞',
  pdf: '▤',
  pptx: '▣',
  folder: '▶',
};

export function WorkspacePage() {
  const { wsId, setWsId, ws, allWs, files } = useWorkspace();

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: '首页', to: '/home' },
          { label: '工作区' },
        ]}
      />

      {/* 3-column shell */}
      <div className="grid grid-cols-[260px_1fr_280px] min-h-0 flex-1 overflow-hidden">

        {/* ── LEFT: Workspace switcher rail (260px) ─────────────── */}
        <aside className="border-r border-line flex flex-col bg-paper overflow-hidden">
          <div className="px-3.5 py-3 border-b border-line-2 flex flex-col gap-1">
            <div className="cw-label mb-1">工作区</div>

            {allWs.map((w) => {
              const active = w.id === wsId;
              return (
                <div
                  key={w.id}
                  onClick={() => setWsId(w.id)}
                  className="relative flex items-center gap-2.5 px-2 py-[7px] rounded-[var(--r-sm)] cursor-pointer transition-colors"
                  style={
                    {
                      '--ws-accent': w.accent,
                      '--ws-accent-soft': w.accent + '14',
                      '--ws-accent-line': w.accent + '55',
                      border: `1px solid ${active ? w.accent + '55' : 'transparent'}`,
                      background: active ? w.accent + '12' : 'transparent',
                    } as React.CSSProperties
                  }
                >
                  {/* active left bar */}
                  {active && (
                    <span
                      className="absolute left-[-1px] top-[6px] bottom-[6px] w-[2px] rounded-[1px]"
                      style={{ background: w.accent }}
                    />
                  )}

                  <span
                    className="cw-ws-chip sm shrink-0"
                    style={
                      {
                        '--ws-accent': w.accent,
                        '--ws-accent-soft': w.accent + '14',
                        '--ws-accent-line': w.accent + '55',
                      } as React.CSSProperties
                    }
                  >
                    {w.glyph}
                  </span>

                  <div className="flex-1 min-w-0">
                    <div
                      className="text-[13px] tracking-[-0.01em] truncate"
                      style={{ fontWeight: active ? 600 : 500 }}
                    >
                      {w.name}
                    </div>
                    <div className="font-mono text-[10px] text-dim truncate">
                      {w.fileCount} 个文件 · {w.taskCount} 个任务
                    </div>
                  </div>
                </div>
              );
            })}

            {/* New workspace placeholder */}
            <div
              className="flex items-center gap-2.5 px-2 py-[7px] mt-1 rounded-[var(--r-sm)] border border-dashed border-line text-ink-2 cursor-not-allowed opacity-60 select-none"
              title="暂无新建工作区 API"
            >
              <span className="cw-ws-chip sm shrink-0" style={{ background: 'var(--paper-2)', borderColor: 'var(--line)', color: 'var(--dim)' } as React.CSSProperties}>
                +
              </span>
              <span className="text-[13px]">新建工作区</span>
            </div>
          </div>

          {/* About section in left rail */}
          <div className="p-3 flex-1 overflow-auto">
            <div className="cw-label mb-2">关于</div>
            <div
              className="p-2.5 bg-surface border border-line rounded-[var(--r-sm)] text-[12.5px] text-ink-2 leading-[1.5]"
            >
              <div className="font-display text-[15px] text-ink mb-1">{ws.name}</div>
              <div className="mb-2">{ws.desc}</div>
              <div className="font-mono text-dim text-[10.5px] leading-[1.7]">
                <div>id · {ws.id}</div>
                <div>{ws.fileCount} 个基础文件</div>
                <div>{ws.taskCount} 个活跃任务</div>
              </div>
            </div>
          </div>
        </aside>

        {/* ── CENTER: Base files list (1fr) ─────────────────────── */}
        <section className="flex flex-col min-w-0 border-r border-line overflow-hidden">
          {/* Section header */}
          <div className="px-[22px] py-3.5 border-b border-line-2">
            <div className="flex items-baseline gap-2.5 mb-1.5 flex-wrap">
              <h1 className="m-0 font-display text-[22px] font-normal tracking-[-0.02em] whitespace-nowrap">
                基础文件
              </h1>
              <Chip dashed>始终作为上下文载入</Chip>
            </div>
            <p className="m-0 text-[12.5px] text-dim leading-[1.5]">
              在 <b className="text-ink-2">{ws.name}</b> 中启动的每个任务都会将这些文件作为基础上下文——加上任务开始时上传的文件。
            </p>
          </div>

          {/* Column header */}
          <div
            className="grid px-[22px] py-1.5 border-b border-line-2 gap-[10px] items-center"
            style={{ gridTemplateColumns: '22px 1fr 88px 64px' }}
          >
            <span />
            <span className="cw-label">名称 · 描述</span>
            <span className="cw-label text-right">大小</span>
            <span className="cw-label text-right">更新</span>
          </div>

          {/* File rows */}
          <div className="flex-1 overflow-auto cw-scroll">
            {files.length === 0 && (
              <div className="px-[22px] py-8 text-center text-dim text-[12.5px] font-mono">
                此工作区暂无基础文件
              </div>
            )}

            {files.map((f) => (
              <div
                key={f.n}
                className="group grid px-[22px] py-[10px] gap-[10px] items-center border-b border-line-2 cursor-default hover:bg-paper-2 transition-colors"
                style={{ gridTemplateColumns: '22px 1fr 88px 64px' }}
              >
                <span className="font-mono text-dim text-[12px]">
                  {FILE_GLYPH[f.k] ?? '·'}
                </span>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[12.5px] text-ink font-medium whitespace-nowrap overflow-hidden text-ellipsis">
                      {f.n}
                    </span>
                    {f.pinned && (
                      <span className="font-mono text-[9.5px] tracking-[0.08em]" style={{ color: ws.accent }}>
                        ● 已固定
                      </span>
                    )}
                  </div>
                  {f.desc && (
                    <div className="font-mono text-[11px] text-dim truncate mt-0.5">{f.desc}</div>
                  )}
                </div>
                <span className="font-mono text-[11px] text-dim text-right">{f.size}</span>
                <span className="font-mono text-[11px] text-dim text-right">{f.updated}</span>
              </div>
            ))}

            {/* Dashed drop-zone placeholder (cosmetic) */}
            <div className="mx-[22px] my-4 border border-dashed border-line rounded-[var(--r-sm)] px-4 py-5 flex items-center justify-center gap-2 text-dim text-[12px] font-mono select-none">
              <span>拖入添加文件</span>
              <Chip dashed>占位 · 待接入</Chip>
            </div>
          </div>

          {/* Section label with count */}
          <div className="px-[22px] py-2 border-t border-line-2 bg-paper">
            <span className="cw-label">基础文件 · {files.length}</span>
          </div>
        </section>

        {/* ── RIGHT: Workspace detail rail (280px) ──────────────── */}
        <aside className="flex flex-col bg-paper overflow-hidden">
          <div className="px-4 py-3.5 border-b border-line-2">
            <div className="cw-label mb-3">工作区详情</div>

            {/* Large ws-chip glyph */}
            <div className="flex items-center gap-3 mb-3">
              <span
                className="cw-ws-chip lg shrink-0"
                style={
                  {
                    '--ws-accent': ws.accent,
                    '--ws-accent-soft': ws.accent + '14',
                    '--ws-accent-line': ws.accent + '55',
                  } as React.CSSProperties
                }
              >
                {ws.glyph}
              </span>
              <div className="min-w-0">
                <div className="font-display text-[17px] tracking-[-0.02em] truncate">{ws.name}</div>
                <div className="text-[12px] text-ink-2 truncate mt-0.5">{ws.desc}</div>
              </div>
            </div>

            {/* Accent swatch */}
            <div className="flex items-center gap-2 mb-4">
              <span
                className="inline-block w-4 h-4 rounded-[3px] border shrink-0"
                style={{ background: ws.accent + '33', borderColor: ws.accent + '55' }}
              />
              <span className="font-mono text-[11px] text-dim">{ws.accent}</span>
            </div>

            {/* Count chips */}
            <div className="flex flex-wrap gap-2">
              <Chip>
                <span className="font-mono text-[11px]">{ws.fileCount} 个文件</span>
              </Chip>
              <Chip>
                <span className="font-mono text-[11px]">{ws.taskCount} 个任务</span>
              </Chip>
            </div>
          </div>

          {/* Files summary */}
          <div className="px-4 py-3 flex-1 overflow-auto cw-scroll">
            <div className="cw-label mb-2">基础文件摘要</div>
            <div className="flex flex-col gap-1">
              {files.map((f) => (
                <div
                  key={f.n}
                  className="flex items-center gap-2 py-1.5 px-2 rounded-[var(--r-sm)] border border-transparent hover:border-line hover:bg-surface transition-colors cursor-default"
                >
                  <span className="font-mono text-dim text-[11px] shrink-0 w-5 text-center">
                    {FILE_GLYPH[f.k] ?? '·'}
                  </span>
                  <span className="font-mono text-[11.5px] text-ink truncate flex-1">{f.n}</span>
                  {f.pinned && (
                    <span
                      className="font-mono text-[9px] tracking-[0.08em] shrink-0"
                      style={{ color: ws.accent }}
                    >
                      ●
                    </span>
                  )}
                </div>
              ))}
              {files.length === 0 && (
                <div className="text-dim text-[12px] font-mono py-4 text-center">暂无文件</div>
              )}
            </div>

            <div className="mt-4 pt-3 border-t border-line-2">
              <div className="cw-label mb-2">工作区 ID</div>
              <code className="font-mono text-[11px] text-dim bg-surface border border-line rounded px-1.5 py-0.5 select-all">
                {ws.id}
              </code>
            </div>

            <div className="mt-3 pt-3 border-t border-line-2">
              <div className="text-[11px] text-dim font-mono italic">
                设置 · 成员 · 新建工作区 = 待接入后端 API
              </div>
            </div>
          </div>
        </aside>

      </div>
    </div>
  );
}
