/**
 * ConfirmCard — HITL tool-call confirmation card.
 *
 * Displayed inline at the bottom of the message list when the agent pauses to
 * request user approval of a tool call.  Shows tool name, a collapsible
 * pretty-printed args view, and 批准 / 拒绝 buttons.
 *
 * Keyboard:
 *   ArrowUp / ArrowDown — move focus between the two action buttons
 *   Enter               — activate the focused button
 *
 * Design tokens: mirrors RewindConfirmModal / output-file cards styling
 * (border-line, bg-paper, accent tokens, cw-btn classes).
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from '../../i18n';
import { api, ApiError } from '../../api/client';
import { useChatStore } from '../../stores/chatStore';
import type { PendingInteraction } from '../../api/types';

interface ConfirmCardProps {
  sessionId: string;
  interaction: PendingInteraction;
}

/**
 * Parse a JSON string into a formatted { key: value } list.
 * Returns null when the string is not valid JSON or is empty.
 */
function parseArgs(args: string | undefined): Record<string, unknown> | null {
  if (!args) return null;
  try {
    const parsed = JSON.parse(args);
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    return { value: parsed };
  } catch {
    return null;
  }
}

export function ConfirmCard({ sessionId, interaction }: ConfirmCardProps) {
  const { t } = useTranslation();
  const removePendingInteraction = useChatStore((s) => s.removePendingInteraction);
  const addPendingInteraction = useChatStore((s) => s.addPendingInteraction);
  const markPendingInteractionAcknowledged = useChatStore(
    (s) => s.markPendingInteractionAcknowledged,
  );
  const unmarkPendingInteractionAcknowledged = useChatStore(
    (s) => s.unmarkPendingInteractionAcknowledged,
  );

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [argsOpen, setArgsOpen] = useState(false);

  // Focus management for keyboard ArrowUp/Down navigation.
  const approveRef = useRef<HTMLButtonElement>(null);
  const rejectRef = useRef<HTMLButtonElement>(null);

  const parsedArgs = parseArgs(interaction.args);

  // Truncate individual arg values for compact display.
  function formatArgValue(v: unknown): string {
    const s = typeof v === 'string' ? v : JSON.stringify(v);
    return s.length > 80 ? `${s.slice(0, 80)}…` : s;
  }

  async function handleAction(confirmed: boolean) {
    if (busy) return;
    setBusy(true);
    setError('');

    // Optimistic removal — card disappears immediately and cannot be
    // resurrected by the replayed pending_confirmation while submit is in flight.
    markPendingInteractionAcknowledged(
      sessionId,
      interaction.runId,
      interaction.toolCallId,
    );
    removePendingInteraction(sessionId, interaction.toolCallId);

    try {
      await api.sessions.respond(sessionId, interaction.runId, {
        kind: 'confirm',
        tool_call_id: interaction.toolCallId,
        confirmed,
        answer: null,
      });
      // Success — run_started from the resumed leg will handle isStreaming.
    } catch (err) {
      // Re-add the interaction so the user can retry.
      unmarkPendingInteractionAcknowledged(
        sessionId,
        interaction.runId,
        interaction.toolCallId,
      );
      addPendingInteraction(sessionId, interaction);
      if (err instanceof ApiError && err.status === 409) {
        setError(t('hitl.error.notPaused'));
      } else if (err instanceof ApiError && err.status === 404) {
        setError(t('hitl.error.pauseExpired'));
      } else {
        setError(t('hitl.error.submitFailed'));
      }
    } finally {
      setBusy(false);
    }
  }

  // Keyboard: ArrowUp / ArrowDown between the two buttons.
  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      rejectRef.current?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      approveRef.current?.focus();
    }
  }

  // Auto-focus the approve button when the card mounts.
  useEffect(() => {
    approveRef.current?.focus();
  }, []);

  return (
    <div
      style={{
        border: '1.5px solid var(--line)',
        borderRadius: 'var(--r-md)',
        background: 'var(--paper)',
        padding: '12px 14px',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      }}
      onKeyDown={handleKeyDown}
    >
      {/* Header: tool name */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: 'var(--accent)',
            flexShrink: 0,
          }}
        />
        <span
          className="font-mono"
          style={{ fontSize: 12, color: 'var(--accent)', fontWeight: 600 }}
        >
          {interaction.name || t('hitl.confirm.unknownTool')}
        </span>
        <span style={{ fontSize: 12, color: 'var(--ink-2)' }}>
          {t('hitl.confirm.awaitingApproval')}
        </span>
      </div>

      {/* Args: compact key-value or collapsible raw */}
      {interaction.args && (
        <div
          style={{
            border: '1px solid var(--line-2)',
            borderRadius: 'var(--r-sm)',
            background: 'var(--paper-2)',
            overflow: 'hidden',
          }}
        >
          {parsedArgs && Object.keys(parsedArgs).length > 0 && (
            <div style={{ padding: '8px 10px' }}>
              {Object.entries(parsedArgs).map(([k, v]) => (
                <div
                  key={k}
                  style={{
                    display: 'flex',
                    gap: 8,
                    fontSize: 12,
                    lineHeight: 1.5,
                    marginBottom: 2,
                  }}
                >
                  <span
                    className="font-mono"
                    style={{ color: 'var(--ink-2)', flexShrink: 0, minWidth: 80 }}
                  >
                    {k}
                  </span>
                  <span
                    className="font-mono"
                    style={{ color: 'var(--ink)', wordBreak: 'break-all' }}
                  >
                    {formatArgValue(v)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {/* Collapsible raw JSON */}
          <button
            type="button"
            onClick={() => setArgsOpen((v) => !v)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 4,
              width: '100%',
              padding: '4px 10px',
              background: 'transparent',
              border: 'none',
              borderTop: '1px solid var(--line-2)',
              cursor: 'pointer',
              fontSize: 11,
              color: 'var(--ink-2)',
              textAlign: 'left',
            }}
          >
            <span className="font-mono" style={{ fontSize: 10 }}>
              {argsOpen ? '▾' : '▸'}
            </span>
            {t('hitl.confirm.rawArgs')}
          </button>
          {argsOpen && (
            <pre
              style={{
                margin: 0,
                padding: '8px 10px',
                fontFamily: 'var(--font-mono, monospace)',
                fontSize: 11,
                color: 'var(--ink-2)',
                overflowX: 'auto',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
                borderTop: '1px solid var(--line-2)',
              }}
            >
              {JSON.stringify(JSON.parse(interaction.args || '{}'), null, 2)}
            </pre>
          )}
        </div>
      )}

      {/* Error line */}
      {error && (
        <div
          style={{
            padding: '6px 10px',
            borderRadius: 'var(--r-sm)',
            border: '1px solid var(--error-line, #fca5a5)',
            background: 'var(--error-soft, #fef2f2)',
            fontSize: 12.5,
            color: 'var(--error, #dc2626)',
          }}
        >
          {error}
        </div>
      )}

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button
          ref={rejectRef}
          type="button"
          className="cw-btn cw-btn-sm"
          onClick={() => handleAction(false)}
          disabled={busy}
          aria-label={t('hitl.confirm.reject')}
        >
          {t('hitl.confirm.reject')}
        </button>
        <button
          ref={approveRef}
          type="button"
          className="cw-btn cw-btn-accent cw-btn-sm"
          onClick={() => handleAction(true)}
          disabled={busy}
          aria-label={t('hitl.confirm.approve')}
        >
          {busy ? t('hitl.confirm.submitting') : t('hitl.confirm.approve')}
        </button>
      </div>
    </div>
  );
}
