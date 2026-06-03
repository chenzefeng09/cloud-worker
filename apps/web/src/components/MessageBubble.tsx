import { useState } from 'react';
import type { Message } from '../api/types';
import ReactMarkdown from 'react-markdown';

interface MessageBubbleProps {
  message: Message;
  friendly?: boolean;
}

export function MessageBubble({ message, friendly }: MessageBubbleProps) {
  // toolExpanded tracks per-tool-call row expansion within a TOOL message.
  // key = index string of each tool_call entry.
  const [toolExpanded, setToolExpanded] = useState<Record<string, boolean>>({});
  // groupOpen tracks whether the entire tool group is expanded.
  const [groupOpen, setGroupOpen] = useState(() => !friendly);

  const isUser = message.role === 'USER';

  // ── USER ─────────────────────────────────────────────────────────────────
  if (isUser) {
    return (
      <div className="flex gap-3 items-start">
        <span className="cw-avatar shrink-0" style={{ marginTop: 2 }}>Y</span>
        <div
          className="flex-1 text-[14px] leading-[1.55] tracking-[-0.005em]"
          style={{
            background: 'var(--paper-2)',
            padding: '10px 14px',
            borderRadius: 'var(--r-md)',
            border: '1px solid var(--line-2)',
          }}
        >
          {message.content}
        </div>
      </div>
    );
  }

  const agentAvatar = (
    <span
      className="cw-avatar accent lg shrink-0 font-mono"
      style={{ marginTop: 2 }}
    >
      ◆
    </span>
  );

  // ── TOOL ─────────────────────────────────────────────────────────────────
  if (message.role === 'TOOL') {
    // Derive tool items from tool_calls or fall back to content snippet
    const toolCalls = message.tool_calls as
      | { name?: string; id?: string; input?: Record<string, unknown> }[]
      | null
      | undefined;
    const itemCount = toolCalls?.length ?? 1;

    return (
      <div className="flex gap-3">
        {agentAvatar}
        <div className="flex-1 min-w-0">
          {/* group header */}
          <div
            onClick={() => setGroupOpen((v) => !v)}
            className="inline-flex items-center gap-2 cursor-pointer text-dim"
            style={
              friendly
                ? {
                    padding: '6px 10px',
                    border: '1px solid var(--line-2)',
                    borderRadius: 'var(--r-sm)',
                    background: 'var(--paper)',
                  }
                : { padding: '4px 8px' }
            }
          >
            <span className="font-mono text-[10.5px]">{groupOpen ? '▾' : '▸'}</span>
            <span className="cw-label">
              {friendly
                ? `worked through ${itemCount} step${itemCount !== 1 ? 's' : ''}`
                : `${itemCount} tool call${itemCount !== 1 ? 's' : ''}`}
            </span>
            {friendly && !groupOpen && toolCalls && (
              <span className="font-mono text-[10.5px] text-dim">
                — {toolCalls.slice(0, 2).map((tc) => tc.name ?? 'tool').join(', ')}
                {toolCalls.length > 2 ? '…' : ''}
              </span>
            )}
          </div>

          {groupOpen && (
            <div className="flex flex-col gap-1 mt-1">
              {toolCalls && toolCalls.length > 0 ? (
                toolCalls.map((tc, i) => {
                  const key = String(i);
                  const exp = toolExpanded[key];
                  const toolName = tc.name ?? 'tool';
                  const toolTarget = tc.input
                    ? String(Object.values(tc.input)[0] ?? '')
                    : '';
                  return (
                    <div key={i}>
                      <div
                        onClick={() =>
                          setToolExpanded((prev) => ({ ...prev, [key]: !exp }))
                        }
                        className="flex items-center gap-2 cursor-pointer font-mono text-[12px]"
                        style={{
                          padding: '6px 10px',
                          border: '1px solid var(--line)',
                          borderRadius: 'var(--r-sm)',
                          background: exp ? 'var(--surface)' : 'var(--paper)',
                        }}
                      >
                        <span className="text-dim text-[10.5px]">
                          {exp ? '▾' : '▸'}
                        </span>
                        <span
                          style={{
                            fontWeight: 600,
                            color:
                              toolName === 'edit' || toolName === 'write'
                                ? 'var(--accent)'
                                : 'var(--ink)',
                          }}
                        >
                          {toolName}
                        </span>
                        {toolTarget && (
                          <span className="text-ink-2 truncate">{toolTarget}</span>
                        )}
                      </div>
                      {exp && (
                        <div
                          className="mt-1 ml-4 font-mono text-[11.5px] text-ink-2 leading-[1.55] whitespace-pre-wrap overflow-x-auto"
                          style={{
                            padding: '10px 12px',
                            background: 'var(--paper-2)',
                            border: '1px solid var(--line-2)',
                            borderRadius: 'var(--r-sm)',
                          }}
                        >
                          {JSON.stringify(tc.input ?? {}, null, 2)}
                        </div>
                      )}
                    </div>
                  );
                })
              ) : (
                /* fallback: show raw content when no structured tool_calls */
                <div
                  className="border border-line-2 rounded-md overflow-hidden"
                >
                  <pre className="px-4 py-3 font-mono text-[11.5px] text-ink-2 leading-[1.6] overflow-x-auto bg-surface whitespace-pre-wrap">
                    {message.content}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // ── SYSTEM ────────────────────────────────────────────────────────────────
  if (message.role === 'SYSTEM') {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 border border-dashed border-line rounded-md font-mono text-[11px] text-dim">
        <span className="text-[9px]">◆</span>
        <span>{message.content}</span>
      </div>
    );
  }

  // ── ASSISTANT ─────────────────────────────────────────────────────────────
  return (
    <div className="flex gap-3">
      {agentAvatar}
      <div className="flex-1 min-w-0" style={{ paddingTop: 3 }}>
        <div className="prose-cw text-[14px] leading-[1.6] text-ink">
          <ReactMarkdown
            components={{
              code: ({ className, children, ...props }) => {
                const isBlock = className?.includes('language-');
                if (isBlock) {
                  return (
                    <pre className="my-3 rounded-md border border-line overflow-x-auto bg-surface">
                      <code className="block px-4 py-3 font-mono text-[12.5px] text-ink-2 leading-[1.6]">
                        {children}
                      </code>
                    </pre>
                  );
                }
                return (
                  <code
                    {...props}
                    className="px-1.5 py-0.5 rounded font-mono text-[12.5px] bg-paper-2 border border-line-2 text-ink"
                  >
                    {children}
                  </code>
                );
              },
              p: ({ children }) => (
                <p className="mb-3 last:mb-0">{children}</p>
              ),
              h1: ({ children }) => (
                <h1 className="mt-4 mb-2 text-[18px] font-semibold tracking-[-0.02em]">
                  {children}
                </h1>
              ),
              h2: ({ children }) => (
                <h2 className="mt-4 mb-2 text-[15px] font-semibold tracking-[-0.01em]">
                  {children}
                </h2>
              ),
              h3: ({ children }) => (
                <h3 className="mt-3 mb-1.5 text-[13.5px] font-semibold">{children}</h3>
              ),
              ul: ({ children }) => (
                <ul className="mb-3 pl-4 list-disc space-y-1">{children}</ul>
              ),
              ol: ({ children }) => (
                <ol className="mb-3 pl-4 list-decimal space-y-1">{children}</ol>
              ),
              li: ({ children }) => (
                <li className="text-[14px]">{children}</li>
              ),
              blockquote: ({ children }) => (
                <blockquote className="pl-3 border-l-2 border-accent text-ink-2 italic my-2">
                  {children}
                </blockquote>
              ),
              a: ({ href, children }) => (
                <a
                  href={href}
                  className="text-accent underline underline-offset-2 hover:opacity-80"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {children}
                </a>
              ),
            }}
          >
            {message.content}
          </ReactMarkdown>
        </div>
        {message.tool_calls && (
          <div className="mt-2">
            <button
              onClick={() => setGroupOpen((v) => !v)}
              className="flex items-center gap-1.5 font-mono text-[10.5px] text-dim hover:text-ink-2"
            >
              <span className="text-[8px]">{groupOpen ? '▾' : '▸'}</span>
              工具调用详情
            </button>
            {groupOpen && (
              <pre className="mt-1 px-3 py-2 font-mono text-[11px] text-dim bg-paper-2 border border-line-2 rounded-md overflow-x-auto">
                {JSON.stringify(message.tool_calls, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
