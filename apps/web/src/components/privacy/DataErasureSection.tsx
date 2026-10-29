import { useId, useState } from 'react';
import { ERASE_CONFIRMATION_PHRASE, errorDetails, privacyApi, type ErasureResult } from '../../api/privacy';
import { clearLocalUserContent } from '../../lib/localUserData';
import { ErasurePartialDetails, ErasureSummary } from './ErasureSummary';
import { btn, describeError, httpStatus, InlineAlert, PrivacySection, Prose } from './ui';

type EraseState =
  | { phase: 'idle' }
  | { phase: 'working' }
  | { phase: 'done'; result: ErasureResult }
  | { phase: 'partial'; details: unknown }
  | { phase: 'error'; message: string };

/** Full reload: every cached list (tasks, files, workspaces) is now stale. */
function reloadHome() {
  window.location.assign('/home');
}

/**
 * "Delete your data" — POST /me/privacy/erase with the typed phrase (GDPR Art. 17).
 * A 502 means some steps finished and others didn't; retrying is safe.
 *
 * @param onFinished called from "Continue" after a successful erasure
 *   (defaults to a full reload of /home).
 */
export function DataErasureSection({ onFinished = reloadHome }: { onFinished?: () => void }) {
  const inputId = useId();
  const [typed, setTyped] = useState('');
  const [state, setState] = useState<EraseState>({ phase: 'idle' });
  const matches = typed.trim() === ERASE_CONFIRMATION_PHRASE;
  const working = state.phase === 'working';
  const done = state.phase === 'done';

  const handleErase = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!matches || working) return;
    setState({ phase: 'working' });
    try {
      const result = await privacyApi.eraseMyData(ERASE_CONFIRMATION_PHRASE);
      // Drafts of the erased tasks kept in this browser go with them.
      clearLocalUserContent();
      setState({ phase: 'done', result });
    } catch (err) {
      const status = httpStatus(err);
      if (status === 502) {
        setState({ phase: 'partial', details: errorDetails(err) });
      } else if (status === 422) {
        setState({ phase: 'error', message: `The confirmation didn't match. Type ${ERASE_CONFIRMATION_PHRASE} exactly, in capitals.` });
      } else {
        setState({ phase: 'error', message: describeError(err, "Couldn't delete your data. It's safe to try again.") });
      }
    }
  };

  if (done) {
    return (
      <PrivacySection title="Delete your data">
        <InlineAlert tone="success">Your data has been deleted.</InlineAlert>
        <ErasureSummary result={state.result} />
        <Prose>
          Your sign-in account itself is closed separately by an administrator. Until then you can still sign
          in, but you'll start from an empty account.
        </Prose>
        <div>
          <button type="button" className={btn.primary} onClick={onFinished}>Continue</button>
        </div>
      </PrivacySection>
    );
  }

  return (
    <PrivacySection title="Delete your data">
      <Prose>
        This permanently deletes your tasks and conversations, uploaded and generated files, canvases,
        workspaces, triggers, memories, preferences and API keys, and stops any tasks that are still running. It
        can't be undone, so download your data first if you want a copy.
      </Prose>
      <Prose>Your sign-in account itself isn't deleted here — an administrator closes it separately.</Prose>

      <form onSubmit={handleErase} className="flex flex-col gap-1.5">
        <label htmlFor={inputId} className="text-[12px] text-ink-2">
          Type <span className="font-mono text-ink">{ERASE_CONFIRMATION_PHRASE}</span> to confirm
        </label>
        <div className="flex items-center gap-2 flex-wrap">
          <input
            id={inputId}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            disabled={working}
            autoComplete="off"
            spellCheck={false}
            placeholder={ERASE_CONFIRMATION_PHRASE}
            className="flex-1 min-w-[200px] border border-line rounded-sm px-2.5 py-1.5 text-[13px] font-mono bg-surface text-ink focus:outline-none focus:border-accent disabled:opacity-60"
          />
          <button type="submit" className={btn.danger} disabled={!matches || working}>
            {working ? 'Deleting…' : state.phase === 'partial' ? 'Try again' : 'Delete my data'}
          </button>
        </div>
      </form>

      {working && (
        <InlineAlert tone="info">Deleting your data… this can take a minute. Please keep this page open.</InlineAlert>
      )}
      {state.phase === 'partial' && (
        <>
          <InlineAlert tone="warn">
            Some of your data couldn't be deleted yet. It's safe to try again — anything already deleted stays
            deleted.
          </InlineAlert>
          <ErasurePartialDetails details={state.details} />
        </>
      )}
      {state.phase === 'error' && <InlineAlert tone="error">{state.message}</InlineAlert>}
    </PrivacySection>
  );
}
