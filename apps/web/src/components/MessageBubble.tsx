import { useState } from 'react';
import type { Message, MessageAttachment, ToolCallEntry } from '../api/types';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { ToolRow, ThinkingBlock, markdownComponents } from './chatStepRenderers';
import { useCopied, CopyIcon } from './CopyButton';
import { formatRelativeTimeZh, formatAbsoluteTimeZh } from '../lib/format';
import { sanitizeStreamingMarkdown } from '../lib/sanitizeStreamingMarkdown';
import { useFeatureFlag } from '../hooks/useFeatureFlags';
import type { CanvasOpenSeed } from '../lib/canvasObjects';

interface MessageBubbleProps {
  message: Message;
  friendly?: boolean;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void;
  onOpenCanvas?: (seed: CanvasOpenSeed) => void;
  onRewind?: (message: Message) => void;
  /** True when this is the LAST assistant text message of its run — only then
   *  is the copy button shown, and it copies the whole round's text. */
  showRoundCopy?: boolean;
  /** Concatenated text of all assistant messages in this round. */
  roundCopyText?: string;
}

// ── Live "Working on it…" pill ────────────────────────────────────────────────
// Rendered as a separate message-level component when `isStreaming` is true.

export function WorkingOnItPill() {
  return (
    <div className="min-w-0">
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
  );
}

// ── Shared user-message action-button styling ─────────────────────────────────

const userActionBtnStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  minWidth: 26,
  height: 26,
  padding: '0 6px',
  background: 'transparent',
  border: 'none',
  borderRadius: 'var(--r-sm)',
  cursor: 'pointer',
  color: 'var(--ink-2)',
  fontSize: 13,
  whiteSpace: 'nowrap',
  transition: 'color 0.15s, background 0.1s',
};

const userActionBtnHoverIn = (e: React.MouseEvent<HTMLButtonElement>) => {
  e.currentTarget.style.background = 'var(--paper-2)';
  e.currentTarget.style.color = 'var(--ink)';
};
const userActionBtnHoverOut = (e: React.MouseEvent<HTMLButtonElement>) => {
  e.currentTarget.style.background = 'transparent';
  e.currentTarget.style.color = 'var(--ink-2)';
};

// ── MessageBubble ─────────────────────────────────────────────────────────────

export function MessageBubble({ message, friendly, onPreviewFile, onOpenCanvas, onRewind, showRoundCopy, roundCopyText }: MessageBubbleProps) {
  const [toolExpanded, setToolExpanded] = useState<Record<string, boolean>>({});
  const [groupOpen, setGroupOpen] = useState(() => !friendly);
  const { copied: userCopied, copy: copyUser } = useCopied();
  const showThinking = useFeatureFlag('ai.thinking_ui');

  const role = (message.role || '').toUpperCase();
  const isUser = role === 'USER';
  const blockKind = message.blockKind;

  // ── USER ──────────────────────────────────────────────────────────────────
  if (isUser) {
    const attachments = message.attachments ?? [];
    // Only show rewind for persisted messages (non-temp, have a real sequence).
    const canRewind = !!onRewind && !message.id.startsWith('temp_user_') && message.sequence > 0;
    return (
      // `group` lets the action row respond to hover of the outer container.
      <div className="group flex flex-col items-end gap-0.5">
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

        {/* Action row — hover-reveal. Copy is always available; rewind only for
            persisted messages. Mirrors the assistant copy-button style. */}
        <div
          className="opacity-0 group-hover:opacity-100 transition-opacity duration-150 flex items-center gap-1"
          aria-label="消息操作"
        >
          <button
            onClick={() => copyUser(message.content)}
            aria-label="复制消息"
            title="复制消息"
            style={userActionBtnStyle}
            onMouseEnter={userActionBtnHoverIn}
            onMouseLeave={userActionBtnHoverOut}
          >
            {userCopied ? (
              <span style={{ fontSize: 11, color: 'var(--accent)' }}>已复制</span>
            ) : (
              <CopyIcon />
            )}
          </button>
          {canRewind && (
            <button
              onClick={() => onRewind(message)}
              aria-label="回退到这一步"
              title="回退到这一步（删除此消息及之后的内容，重新编辑）"
              style={userActionBtnStyle}
              onMouseEnter={userActionBtnHoverIn}
              onMouseLeave={userActionBtnHoverOut}
            >
              ↺
            </button>
          )}
        </div>
      </div>
    );
  }

  // ── THINKING block (gated by ai.thinking_ui flag) ─────────────────────────
  if (blockKind === 'thinking') {
    if (!showThinking) return null;
    return (
      <div className="min-w-0">
        <ThinkingBlock content={message.content} friendly={friendly} />
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
        <div className="min-w-0">
          <ToolRow
            tc={tc}
            rowKey={key}
            expanded={!!toolExpanded[key]}
            onToggle={() => setToolExpanded((p) => ({ ...p, [key]: !p[key] }))}
          />
        </div>
      );
    }

    // Grouped (multiple tool calls in one message, e.g. hydrated row).
    const itemCount = toolCalls.length || 1;
    return (
      <div className="min-w-0">
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
    <AssistantTextBlock
      messageId={message.id}
      content={message.content}
      streaming={message.streaming}
      showCopy={showRoundCopy}
      copyText={roundCopyText}
      createdAt={message.created_at}
      onOpenCanvas={onOpenCanvas}
    />
  );
}

// ── Assistant text block with hover copy action row ───────────────────────────

function AssistantTextBlock({
  messageId,
  content,
  streaming,
  showCopy,
  copyText,
  createdAt,
  onOpenCanvas,
}: {
  messageId: string;
  content: string;
  streaming?: boolean;
  showCopy?: boolean;
  copyText?: string;
  createdAt?: string;
  onOpenCanvas?: (seed: CanvasOpenSeed) => void;
}) {
  const { copied, copy } = useCopied();
  const displayContent = streaming ? sanitizeStreamingMarkdown(content) : content;

  return (
    // `group` lets child elements respond to hover of this container.
    <div className="group min-w-0">
      <div className="prose-cw text-[14px] leading-[1.6] text-ink">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
          components={markdownComponents}
        >
          {displayContent}
        </ReactMarkdown>
      </div>

      {/* Action row — shown ONLY on the last assistant message of the round.
          Sits below the text; copies the whole round's assistant text. */}
      {showCopy && (
      <div
        className="opacity-0 group-hover:opacity-100 transition-opacity duration-150 flex items-center gap-1 mt-1"
        aria-label="消息操作"
      >
        {/* Copy button */}
        <button
          onClick={() => copy(copyText ?? content)}
          aria-label={copied ? '已复制' : '复制消息'}
          title={copied ? '已复制' : '复制消息'}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 4,
            width: 26,
            height: 26,
            justifyContent: 'center',
            padding: 0,
            background: 'transparent',
            border: 'none',
            borderRadius: 'var(--r-sm)',
            cursor: 'pointer',
            color: copied ? 'var(--accent)' : 'var(--ink-2)',
            transition: 'color 0.15s, background 0.1s',
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = 'var(--paper-2)';
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
          }}
        >
          {copied ? (
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                color: 'var(--accent)',
                whiteSpace: 'nowrap',
              }}
            >
              已复制
            </span>
          ) : (
            <CopyIcon />
          )}
        </button>
        {/* Timestamp — relative text, absolute time on hover (native tooltip) */}
        {createdAt && (
          <time
            dateTime={createdAt}
            title={formatAbsoluteTimeZh(createdAt)}
            style={{
              fontSize: 11,
              color: 'var(--dim)',
              whiteSpace: 'nowrap',
              cursor: 'default',
            }}
          >
            {formatRelativeTimeZh(createdAt)}
          </time>
        )}
        {onOpenCanvas && (
          <button
            onClick={() =>
              onOpenCanvas({
                kind: 'article',
                content: copyText ?? content,
                sourceMessageId: messageId,
              })
            }
            aria-label="放入画布"
            title="放入画布"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              justifyContent: 'center',
              minWidth: 44,
              height: 26,
              padding: '0 7px',
              background: 'transparent',
              border: 'none',
              borderRadius: 'var(--r-sm)',
              cursor: 'pointer',
              color: 'var(--ink-2)',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              whiteSpace: 'nowrap',
              transition: 'color 0.15s, background 0.1s',
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLButtonElement).style.background = 'var(--paper-2)';
              (e.currentTarget as HTMLButtonElement).style.color = 'var(--ink)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
              (e.currentTarget as HTMLButtonElement).style.color = 'var(--ink-2)';
            }}
          >
            <span>◇</span>
            <span>画布</span>
          </button>
        )}
      </div>
      )}
    </div>
  );
}
