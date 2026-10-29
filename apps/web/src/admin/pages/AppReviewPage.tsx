import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { formatAbsoluteTime } from '../../lib/format';
import type { AppReviewItem, AppVersion, InputField } from '../../api/types';

// ─── Harness detail panel ─────────────────────────────────────────────────────

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">{label}</div>
      <div className="text-[12.5px] text-ink">{children}</div>
    </div>
  );
}

function PromptBlock({ value }: { value: string }) {
  if (!value) return <span className="text-dim italic">(empty)</span>;
  return (
    <pre className="border border-line-2 rounded-sm bg-paper-2 px-3 py-2 text-[12px] text-ink-2 whitespace-pre-wrap font-mono max-h-64 overflow-auto">
      {value}
    </pre>
  );
}

function ChipList({ values }: { values: string[] }) {
  if (!values || values.length === 0) return <span className="text-dim italic">(none)</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {values.map((v) => (
        <span
          key={v}
          className="border border-line rounded-sm px-1.5 py-0.5 font-mono text-[11px] text-ink-2 bg-paper-2"
        >
          {v}
        </span>
      ))}
    </div>
  );
}

const FIELD_TYPE_ZH: Record<InputField['type'], string> = {
  text: 'Single-line text',
  textarea: 'Multi-line text',
  select: 'Single select',
  multiselect: 'Multi-select',
  file: 'File',
};

function InputSchemaTable({ fields }: { fields: InputField[] }) {
  if (!fields || fields.length === 0) {
    return <span className="text-dim italic">(no input fields)</span>;
  }
  return (
    <div className="border border-line rounded-md overflow-hidden">
      <table className="w-full text-left border-collapse">
        <thead className="bg-paper-2 border-b border-line">
          <tr>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Key</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Type</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Label</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Required</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Options / constraints</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => (
            <tr key={f.key} className="border-b border-line-2 last:border-0">
              <td className="px-3 py-2 font-mono text-[12px] text-ink">{f.key}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{FIELD_TYPE_ZH[f.type] ?? f.type}</td>
              <td className="px-3 py-2 text-[12.5px] text-ink">
                {f.label}
                {f.help && <div className="text-[11px] text-dim mt-0.5">{f.help}</div>}
              </td>
              <td className="px-3 py-2 font-mono text-[11px]">
                {f.required ? <span className="text-accent">Yes</span> : <span className="text-dim">No</span>}
              </td>
              <td className="px-3 py-2 text-[11.5px] text-ink-2">
                {(f.type === 'select' || f.type === 'multiselect') && f.options && f.options.length > 0 && (
                  <div className="flex flex-wrap gap-1">
                    {f.options.map((o) => (
                      <span
                        key={o.value}
                        className="border border-line-2 rounded-sm px-1 py-0.5 font-mono text-[10.5px] text-ink-2"
                      >
                        {o.label}
                        <span className="text-dim"> ({o.value})</span>
                      </span>
                    ))}
                  </div>
                )}
                {f.type === 'file' && f.file && (
                  <div className="font-mono text-[10.5px] text-dim">
                    {f.file.accept.join(', ') || 'Any type'} · ≤{Math.round(f.file.max_size_bytes / 1024 / 1024)}MB ·{' '}
                    {f.file.multiple ? 'Multiple files' : 'Single file'}
                  </div>
                )}
                {f.type !== 'select' && f.type !== 'multiselect' && f.type !== 'file' && (
                  <span className="text-dim">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HarnessDetail({ version }: { version: AppVersion }) {
  const sp = version.sampling_params ?? {};
  const samplingText =
    [
      sp.temperature != null ? `temperature=${sp.temperature}` : null,
      sp.top_p != null ? `top_p=${sp.top_p}` : null,
      sp.max_tokens != null ? `max_tokens=${sp.max_tokens}` : null,
    ]
      .filter(Boolean)
      .join('  ') || null;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-4">
        <DetailRow label="Model">
          <span className="font-mono text-[12px]">{version.model}</span>
        </DetailRow>
        <DetailRow label="Reasoning effort">
          <span className="font-mono text-[12px]">{version.reasoning_effort || 'Default'}</span>
        </DetailRow>
      </div>

      <DetailRow label="Sampling parameters">
        {samplingText ? <span className="font-mono text-[12px] text-ink-2">{samplingText}</span> : <span className="text-dim italic">(default)</span>}
      </DetailRow>

      <DetailRow label="System prompt (system_prompt)">
        <PromptBlock value={version.system_prompt} />
      </DetailRow>

      <DetailRow label="User prompt template (user_prompt_template)">
        <PromptBlock value={version.user_prompt_template} />
      </DetailRow>

      <div className="grid grid-cols-2 gap-4">
        <DetailRow label="Required skills">
          <ChipList values={version.forced_skills} />
        </DetailRow>
        <DetailRow label="Required MCP servers">
          <ChipList values={version.forced_mcp_server_ids} />
        </DetailRow>
      </div>

      <DetailRow label="Input schema (input_schema)">
        <InputSchemaTable fields={version.input_schema} />
      </DetailRow>
    </div>
  );
}

// ─── Review modal ─────────────────────────────────────────────────────────────

function ReviewModal({
  item,
  onClose,
  onResolved,
}: {
  item: AppReviewItem;
  onClose: () => void;
  onResolved: (vid: string) => void;
}) {
  const vid = item.version.id;
  const [note, setNote] = useState('');
  const [rejecting, setRejecting] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof ApiError) {
      setError(err.status === 403 ? 'No review permission (admins only)' : err.message);
    } else {
      setError(fallback);
    }
  };

  const handleApprove = async () => {
    setLoading(true);
    setError(null);
    try {
      await api.apps.approve(vid, note.trim() || undefined);
      onResolved(vid);
    } catch (err) {
      fail(err, 'Failed to approve');
      setLoading(false);
    }
  };

  const handleReject = async () => {
    if (!note.trim()) {
      setError('A reason is required to reject');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await api.apps.reject(vid, note.trim());
      onResolved(vid);
    } catch (err) {
      fail(err, 'Failed to reject');
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 p-6 overflow-auto"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-surface border border-line rounded-md w-full max-w-3xl my-6 flex flex-col">
        {/* Header */}
        <div className="flex items-start justify-between px-5 py-4 border-b border-line">
          <div>
            <div className="text-[15px] font-display text-ink tracking-[-0.01em]">{item.app_name}</div>
            <div className="text-[11px] text-dim font-mono mt-0.5">
              Version {item.version.version} · Author {item.author_id} · Submitted {formatAbsoluteTime(item.version.submitted_at)}
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-[12px] font-mono text-dim hover:text-ink transition-colors"
          >
            Close
          </button>
        </div>

        {/* Body */}
        <div className="px-5 py-4 overflow-auto max-h-[60vh]">
          <HarnessDetail version={item.version} />
        </div>

        {/* Footer / actions */}
        <div className="px-5 py-4 border-t border-line flex flex-col gap-3">
          {error && (
            <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
          )}
          {rejecting ? (
            <div className="flex flex-col gap-2">
              <label className="text-[11px] font-mono text-dim">Rejection reason (required)</label>
              <textarea
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-h-[72px] resize-y"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Explain why — the author will see this"
                autoFocus
              />
              <div className="flex gap-2">
                <button
                  onClick={handleReject}
                  disabled={loading}
                  className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
                >
                  {loading ? 'Rejecting…' : 'Confirm rejection'}
                </button>
                <button
                  onClick={() => { setRejecting(false); setError(null); }}
                  disabled={loading}
                  className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
                >
                  Back
                </button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <label className="text-[11px] font-mono text-dim">Review note (optional when approving)</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Optional note"
              />
              <div className="flex gap-2">
                <button
                  onClick={handleApprove}
                  disabled={loading}
                  className="px-3 py-1.5 bg-ok text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
                >
                  {loading ? 'Processing…' : 'Approve'}
                </button>
                <button
                  onClick={() => { setRejecting(true); setNote(''); setError(null); }}
                  disabled={loading}
                  className="px-3 py-1.5 border border-accent-line text-accent text-[12px] font-mono rounded-sm hover:bg-accent-soft disabled:opacity-50"
                >
                  Reject
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function AppReviewPage() {
  const [items, setItems] = useState<AppReviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<AppReviewItem | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const fetchPending = () => {
    setLoading(true);
    setError(null);
    return api.apps
      .reviewPending()
      .then((data) => setItems(data.items))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 403) {
          setError('No review permission (admins only)');
        } else {
          setError(err instanceof ApiError ? err.message : 'Failed to load');
        }
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.apps
      .reviewPending()
      .then((data) => { if (!cancelled) setItems(data.items); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 403) {
          setError('No review permission (admins only)');
        } else {
          setError(err instanceof ApiError ? err.message : 'Failed to load');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const handleResolved = (vid: string) => {
    const resolved = items.find((it) => it.version.id === vid);
    // Optimistically drop the row, then refetch to stay in sync with backend.
    setItems((prev) => prev.filter((it) => it.version.id !== vid));
    setActive(null);
    setFeedback(resolved ? `Processed “${resolved.app_name}” version ${resolved.version.version}` : 'Processed');
    void fetchPending();
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">App review</h1>
          <p className="text-[12px] text-dim mt-0.5">Review app versions submitted for publishing</p>
        </div>
        <button
          onClick={() => void fetchPending()}
          disabled={loading}
          className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
        >
          Refresh
        </button>
      </div>

      {feedback && (
        <div className="border border-line rounded-md px-4 py-2 bg-paper-2 text-[12px] text-ink-2 font-mono">
          {feedback}
        </div>
      )}

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">Loading…</div>
      ) : items.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No apps awaiting review</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">App</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Author</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Version</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Submitted</th>
                <th className="px-3 py-2 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr
                  key={it.version.id}
                  className="border-b border-line-2 hover:bg-paper-2 transition-colors group"
                >
                  <td className="px-3 py-2 text-[13px] text-ink">{it.app_name}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{it.author_id}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{it.version.version}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {formatAbsoluteTime(it.version.submitted_at)}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => { setActive(it); setFeedback(null); }}
                      className="text-[11px] font-mono text-dim hover:text-ink transition-colors"
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {active && (
        <ReviewModal item={active} onClose={() => setActive(null)} onResolved={handleResolved} />
      )}
    </div>
  );
}
