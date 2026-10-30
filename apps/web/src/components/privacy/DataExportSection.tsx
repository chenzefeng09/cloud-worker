import { useState } from 'react';
import { privacyApi } from '../../api/privacy';
import { saveBlob } from './download';
import { formatBytes } from './labels';
import { btn, describeError, InlineAlert, PrivacySection, Prose } from './ui';

type ExportState =
  | { phase: 'idle' }
  | { phase: 'working'; received: number; total: number | null }
  | { phase: 'done'; filename: string; size: number }
  | { phase: 'error'; message: string };

function progressText(received: number, total: number | null): string {
  if (received === 0) return 'Preparing your file…';
  if (total) return `Downloading… ${Math.min(100, Math.round((received / total) * 100))}%`;
  return `Downloading… ${formatBytes(received)}`;
}

/** "Download your data" — GET /me/privacy/export, saved as a JSON file (GDPR Art. 15 / 20). */
export function DataExportSection() {
  const [state, setState] = useState<ExportState>({ phase: 'idle' });

  const handleExport = async () => {
    setState({ phase: 'working', received: 0, total: null });
    try {
      const file = await privacyApi.exportMyData({
        onProgress: (received, total) => setState({ phase: 'working', received, total }),
      });
      saveBlob(file.blob, file.filename);
      setState({ phase: 'done', filename: file.filename, size: file.size });
    } catch (err) {
      setState({ phase: 'error', message: describeError(err, "Couldn't prepare your download. Please try again.") });
    }
  };

  const working = state.phase === 'working';

  return (
    <PrivacySection title="Download your data">
      <Prose>
        Get a copy of the data we hold about you as a JSON file: your profile and preferences, tasks and
        conversations, files and canvases, workspaces, memories, triggers, the apps you made, and API key
        details (never the secret key itself).
      </Prose>
      <div className="flex items-center gap-3 flex-wrap">
        <button type="button" className={btn.primary} onClick={handleExport} disabled={working}>
          {working ? 'Preparing…' : 'Download my data'}
        </button>
        <span role="status" className="font-mono text-[11px] text-dim">
          {state.phase === 'working' && progressText(state.received, state.total)}
          {state.phase === 'done' && `Saved ${state.filename}${state.size ? ` (${formatBytes(state.size)})` : ''}`}
        </span>
      </div>
      {state.phase === 'error' && <InlineAlert tone="error">{state.message}</InlineAlert>}
    </PrivacySection>
  );
}
