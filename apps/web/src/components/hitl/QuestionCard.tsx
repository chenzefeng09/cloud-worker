/**
 * QuestionCard — HITL agent-question response card.
 *
 * Displayed when the agent uses the `ask_user` tool to pause and ask a
 * question.  Shows the question text and a textarea for the answer.
 *
 * Keyboard:
 *   Enter            → submit (same behaviour as the composer)
 *   Shift+Enter      → insert newline (same as composer)
 *
 * Design tokens: mirrors InputBar / RewindConfirmModal styling.
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from '../../i18n';
import { api, ApiError } from '../../api/client';
import { useChatStore } from '../../stores/chatStore';
import type { PendingInteraction } from '../../api/types';

interface QuestionCardProps {
  sessionId: string;
  interaction: PendingInteraction;
}

export function QuestionCard({ sessionId, interaction }: QuestionCardProps) {
  const { t } = useTranslation();
  const removePendingInteraction = useChatStore((s) => s.removePendingInteraction);
  const addPendingInteraction = useChatStore((s) => s.addPendingInteraction);
  const markPendingInteractionAcknowledged = useChatStore(
    (s) => s.markPendingInteractionAcknowledged,
  );
  const unmarkPendingInteractionAcknowledged = useChatStore(
    (s) => s.unmarkPendingInteractionAcknowledged,
  );

  const [answer, setAnswer] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-focus the textarea when the card mounts.
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  async function handleSubmit() {
    const trimmed = answer.trim();
    if (!trimmed || busy) return;

    setBusy(true);
    setError('');

    // Optimistic removal; keep the interaction suppressed while the response is
    // in flight so SSE replay cannot show a second answer card.
    markPendingInteractionAcknowledged(
      sessionId,
      interaction.runId,
      interaction.toolCallId,
    );
    removePendingInteraction(sessionId, interaction.toolCallId);

    try {
      await api.sessions.respond(sessionId, interaction.runId, {
        kind: 'answer',
        tool_call_id: interaction.toolCallId,
        confirmed: null,
        answer: trimmed,
      });
      // Success — run_started from the resumed leg drives isStreaming.
    } catch (err) {
      // Re-add interaction so the user can retry.
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

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
    // Shift+Enter: let the default newline behaviour through.
  }

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
    >
      {/* Header */}
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
        <span style={{ fontSize: 12, color: 'var(--ink-2)' }}>
          {t('hitl.question.label')}
        </span>
      </div>

      {/* Question text */}
      {interaction.question && (
        <p
          style={{
            margin: 0,
            fontSize: 13.5,
            lineHeight: 1.6,
            color: 'var(--ink)',
          }}
        >
          {interaction.question}
        </p>
      )}

      {/* Answer textarea */}
      <textarea
        ref={textareaRef}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={busy}
        rows={3}
        placeholder={t('hitl.question.placeholder')}
        style={{
          width: '100%',
          resize: 'vertical',
          padding: '8px 10px',
          border: '1px solid var(--line)',
          borderRadius: 'var(--r-sm)',
          background: 'var(--paper-2)',
          fontSize: 13,
          color: 'var(--ink)',
          outline: 'none',
          fontFamily: 'inherit',
          boxSizing: 'border-box',
        }}
      />

      {/* Hint */}
      <div style={{ fontSize: 11, color: 'var(--dim)' }}>
        {t('hitl.question.hint')}
      </div>

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

      {/* Submit button */}
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <button
          type="button"
          className="cw-btn cw-btn-accent cw-btn-sm"
          onClick={handleSubmit}
          disabled={busy || !answer.trim()}
        >
          {busy ? t('hitl.question.submitting') : t('hitl.question.submit')}
        </button>
      </div>
    </div>
  );
}
