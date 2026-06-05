import { useState } from 'react';
import type { Message, MessageAttachment, ToolCallEntry } from '../api/types';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ToolRow, ThinkingBlock, markdownComponents } from './chatStepRenderers';

interface MessageBubbleProps {
  message: Message;
  friendly?: boolean;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void;
}

// ── Live "Working on it…" pill ────────────────────────────────────────────────
// Rendered as a separate message-level component when `isStreaming` is true.

export function WorkingOnItPill() {
  return (
    <div className="flex gap-3 items-start">
      <span className="cw-avatar accent lg shrink-0 font-mono">◆</span>
      <div className="flex-1 min-w-0">
        <div
          style={{
            padding: '10px 12px',
            border: '1.5px solid var(--accent-line)',
            background: 'var(--accent-soft)',
            borderRadius: 'var(--r-sm)',
            display: 'flex',
            alignItems: 'flex-start',
            gap: 10,
          }}
        >
          <span className="cw-blip" style={{ marginTop: 5, flexShrink: 0 }} />
          <div
            style={{
              fontSize: 13.5,
              color: 'var(--accent)',
              fontWeight: 600,
              letterSpacing: '-0.005em',
            }}
          >
            Working on it…
          </div>
        </div>
      </div>
    </div>
  );
}

// ── MessageBubble ─────────────────────────────────────────────────────────────

export function MessageBubble({ message, friendly, onPreviewFile }: MessageBubbleProps) {
  const [toolExpanded, setToolExpanded] = useState<Record<string, boolean>>({});
  const [groupOpen, setGroupOpen] = useState(() => !friendly);

  const role = (message.role || '').toUpperCase();
  const isUser = role === 'USER';
  const blockKind = message.blockKind;

  // ── USER ──────────────────────────────────────────────────────────────────
  if (isUser) {
    const attachments = message.attachments ?? [];
    return (
      <div className="flex flex-row-reverse gap-3 items-start">
        <span className="cw-avatar shrink-0 mt-[10px]">Y</span>
        <div
          className="max-w-[80%] text-[14px] leading-[1.55] tracking-[-0.005em]"
          style={{
            background: 'var(--paper-2)',
            padding: '10px 14px',
            borderRadius: 'var(--r-md)',
            border: '1px solid var(--line-2)',
          }}
        >
          {message.content}
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {attachments.map((att: MessageAttachment) => (
                <div
                  key={att.id}
                  className="flex items-center gap-1 px-2 py-1 border border-line rounded-md text-[11px] bg-surface cursor-pointer hover:bg-paper-2"
                  role="button"
                  onClick={() => onPreviewFile?.({ id: att.id, filename: att.filename, mime: att.mime })}
                >
                  <span className="text-ink-2">
                    {att.mime?.startsWith('image/') ? '🖼' : '📎'}
                  </span>
                  <span className="max-w-[120px] truncate text-ink">{att.filename}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  const agentAvatar = (
    <span className="cw-avatar accent lg shrink-0 font-mono">◆</span>
  );

  // ── THINKING block ────────────────────────────────────────────────────────
  if (blockKind === 'thinking') {
    return (
      <div className="flex gap-3 items-start">
        {agentAvatar}
        <div className="flex-1 min-w-0">
          <ThinkingBlock content={message.content} friendly={friendly} />
        </div>
      </div>
    );
  }

  // ── TOOL block (both old grouped TOOL role and new per-call tool blocks) ──
  if (role === 'TOOL' || blockKind === 'tool') {
    const toolCalls = (message.tool_calls ?? []) as ToolCallEntry[];

    // New ordered pipeline: single tool_call per block (blockKind === 'tool').
    // Old hydrated rows may carry multiple tool_calls in one message.
    if (blockKind === 'tool' && toolCalls.length <= 1) {
      // Single tool row — render without the group header.
      const tc = toolCalls[0];
      if (!tc) return null;
      const key = tc.id;
      return (
        <div className="flex gap-3 items-start">
          {agentAvatar}
          <div className="flex-1 min-w-0">
            <ToolRow
              tc={tc}
              rowKey={key}
              expanded={!!toolExpanded[key]}
              onToggle={() => setToolExpanded((p) => ({ ...p, [key]: !p[key] }))}
            />
          </div>
        </div>
      );
    }

    // Grouped (multiple tool calls in one message, e.g. hydrated row).
    const itemCount = toolCalls.length || 1;
    return (
      <div className="flex gap-3 items-start">
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
            {friendly && !groupOpen && toolCalls.length > 0 && (
              <span className="font-mono text-[10.5px] text-dim">
                — {toolCalls.slice(0, 2).map((tc) => tc.name ?? 'tool').join(', ')}
                {toolCalls.length > 2 ? '…' : ''}
              </span>
            )}
          </div>

          {groupOpen && (
            <div className="flex flex-col gap-1 mt-1">
              {toolCalls.length > 0 ? (
                toolCalls.map((tc) => {
                  const key = tc.id;
                  return (
                    <ToolRow
                      key={key}
                      tc={tc}
                      rowKey={key}
                      expanded={!!toolExpanded[key]}
                      onToggle={() =>
                        setToolExpanded((p) => ({ ...p, [key]: !p[key] }))
                      }
                    />
                  );
                })
              ) : (
                <div className="border border-line-2 rounded-md overflow-hidden">
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
  if (role === 'SYSTEM') {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 border border-dashed border-line rounded-md font-mono text-[11px] text-dim">
        <span className="text-[9px]">◆</span>
        <span>{message.content}</span>
      </div>
    );
  }

  // ── ASSISTANT (text block) ────────────────────────────────────────────────
  return (
    <div className="flex gap-3 items-start">
      {agentAvatar}
      <div className="flex-1 min-w-0">
        <div className="prose-cw text-[14px] leading-[1.6] text-ink">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {message.content}
          </ReactMarkdown>
        </div>
      </div>
    </div>
  );
}
