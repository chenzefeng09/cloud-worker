import { useId, useState } from 'react';
import { errorDetails, privacyApi, type ErasureResult } from '../../api/privacy';
import { usePrompt, useToast } from '../../admin/components/dialogs';
import { saveBlob } from './download';
import { ErasurePartialDetails, ErasureSummary } from './ErasureSummary';
import { formatBytes } from './labels';
import { describeError, httpStatus, InlineAlert } from './ui';

type PanelState =
  | { kind: 'idle' }
  | { kind: 'exporting'; userId: string; received: number }
  | { kind: 'exported'; userId: string; filename: string; size: number }
  | { kind: 'erasing'; userId: string }
  | { kind: 'erased'; userId: string; result: ErasureResult }
  | { kind: 'partial'; userId: string; details: unknown }
  | { kind: 'error'; message: string };

const inputCls =
  'border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono';
const outlineBtn =
  'px-2.5 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50 disabled:cursor-not-allowed';
const dangerBtn =
  'px-2.5 py-1.5 text-[11px] font-mono border border-accent-line rounded-sm text-accent hover:bg-accent-soft disabled:opacity-50 disabled:cursor-not-allowed';

function failureMessage(err: unknown, userId: string, fallback: string): string {
  const status = httpStatus(err);
  if (status === 404) return `No user found with ID ${userId}.`;
  if (status === 403) return "You don't have permission to do this.";
  return describeError(err, fallback);
}

/**
 * Admin: fulfil a data subject request for one user — export everything the
 * platform holds about them (Art. 15 / 20) or erase it (Art. 17). Erasure needs
 * the user id typed back as confirmation; a 502 means it only partly finished
 * and can be retried safely.
 */
export function DataSubjectRequestsPanel({ tenantId }: { tenantId?: string }) {
  const prompt = usePrompt();
  const toast = useToast();
  const inputId = useId();
  const [userId, setUserId] = useState('');
  const [state, setState] = useState<PanelState>({ kind: 'idle' });
  const id = userId.trim();
  const busy = state.kind === 'exporting' || state.kind === 'erasing';

  const handleExport = async () => {
    if (!id || busy) return;
    setState({ kind: 'exporting', userId: id, received: 0 });
    try {
      const file = await privacyApi.admin.exportUser(id, {
        tenantId,
        onProgress: (received) => setState({ kind: 'exporting', userId: id, received }),
      });
      saveBlob(file.blob, file.filename);
      setState({ kind: 'exported', userId: id, filename: file.filename, size: file.size });
      toast.success('Export downloaded');
    } catch (err) {
      setState({ kind: 'error', message: failureMessage(err, id, 'Export failed.') });
    }
  };

  const runErase = async (target: string) => {
    setState({ kind: 'erasing', userId: target });
    try {
      const result = await privacyApi.admin.eraseUser(target, { tenantId });
      setState({ kind: 'erased', userId: target, result });
      toast.success('User data erased');
    } catch (err) {
      if (httpStatus(err) === 502) {
        setState({ kind: 'partial', userId: target, details: errorDetails(err) });
      } else if (httpStatus(err) === 422) {
        setState({ kind: 'error', message: "The confirmation didn't match the user ID, so nothing was erased." });
      } else {
        setState({ kind: 'error', message: failureMessage(err, target, 'Erasure failed.') });
      }
    }
  };

  const handleErase = async () => {
    if (!id || busy) return;
    const typed = await prompt({
      title: 'Erase user data',
      description: (
        <>
          This permanently deletes the data user <span className="font-mono text-ink">{id}</span> has on the
          platform: tasks and conversations, files, canvases, workspaces, triggers, memories, preferences and API
          keys. Running tasks are stopped. It can't be undone — export first if the user asked for a copy.
          {'\n\n'}
          Model preferences aren't included: the model gateway only lets users clear their own, so ask the user
          to delete their data themselves if they need those gone too.
          {'\n\n'}
          Their sign-in account isn't closed by this; close it separately (for example, freeze it on the Users
          page).
        </>
      ),
      label: 'Type the user ID to confirm',
      placeholder: id,
      confirmText: 'Erase data',
      validate: (v) => (v.trim() === id ? null : "That doesn't match the user ID."),
    });
    if (typed === null) return;
    await runErase(id);
  };

  return (
    <section className="flex flex-col gap-2" aria-labelledby={`${inputId}-title`}>
      <div id={`${inputId}-title`} className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
        Data subject requests
      </div>
      <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-3">
        <p className="text-[12px] text-ink-2 leading-relaxed">
          Export or erase everything the platform holds about one user, to fulfil access, portability and
          erasure requests (GDPR Art. 15, 20 and 17). Log the request as a DSAR above to keep a record.
        </p>
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <label htmlFor={inputId} className="text-[11px] text-dim font-mono">User ID</label>
            <input
              id={inputId}
              className={`${inputCls} w-[260px]`}
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              disabled={busy}
              placeholder="usr_…"
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <button type="button" className={outlineBtn} onClick={handleExport} disabled={!id || busy}>
            {state.kind === 'exporting' ? 'Exporting…' : 'Export data'}
          </button>
          <button type="button" className={dangerBtn} onClick={handleErase} disabled={!id || busy}>
            {state.kind === 'erasing' ? 'Erasing…' : 'Erase data…'}
          </button>
        </div>

        {state.kind === 'exporting' && (
          <div role="status" className="font-mono text-[11px] text-dim">
            {state.received > 0
              ? `Downloading… ${formatBytes(state.received)}`
              : `Preparing the export for ${state.userId}…`}
          </div>
        )}
        {state.kind === 'exported' && (
          <InlineAlert tone="success">
            Downloaded <span className="font-mono">{state.filename}</span>
            {state.size ? ` (${formatBytes(state.size)})` : ''} for <span className="font-mono">{state.userId}</span>.
          </InlineAlert>
        )}
        {state.kind === 'erasing' && (
          <div role="status" className="font-mono text-[11px] text-dim">Erasing data for {state.userId}…</div>
        )}
        {state.kind === 'erased' && (
          <>
            <InlineAlert tone="success">
              Erased the data held for <span className="font-mono">{state.userId}</span>.
            </InlineAlert>
            <ErasureSummary result={state.result} />
          </>
        )}
        {state.kind === 'partial' && (
          <>
            <InlineAlert tone="warn">
              Erasure for <span className="font-mono">{state.userId}</span> only partly finished. It's safe to
              retry — anything already deleted stays deleted.
            </InlineAlert>
            <ErasurePartialDetails details={state.details} />
            <div>
              <button type="button" className={dangerBtn} onClick={() => runErase(state.userId)}>
                Retry erasure
              </button>
            </div>
          </>
        )}
        {state.kind === 'error' && <InlineAlert tone="error">{state.message}</InlineAlert>}
      </div>
    </section>
  );
}
