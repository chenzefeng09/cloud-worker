import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useToast } from '../components/dialogs';
import { CustomSelect, ComboBox } from '../components/CustomSelect';
import type { NotificationSendRequest, NotificationTemplate } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

export function NotificationAdminPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();

  const [templates, setTemplates] = useState<NotificationTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Send-test form state.
  const [eventType, setEventType] = useState('');
  const [recipients, setRecipients] = useState('');
  const [channel, setChannel] = useState('email');
  const [template, setTemplate] = useState('');
  const [priority, setPriority] = useState('normal');
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  // Bootstrap tenant list.
  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.notification
      .templates(selectedTenantId ?? undefined)
      .then((items) => setTemplates(items))
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
      })
      .finally(() => setLoading(false));
  }, [selectedTenantId]);

  useEffect(() => { load(); }, [load]);

  // Distinct, non-empty event types drawn from loaded templates.
  const eventTypeOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: { value: string; label: string }[] = [];
    for (const t of templates) {
      const et = t.event_type?.trim();
      if (et && !seen.has(et)) {
        seen.add(et);
        out.push({ value: et, label: et });
      }
    }
    return out;
  }, [templates]);

  // Template options, filtered by the selected event type when one is set.
  const templateOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: { value: string; label: string }[] = [];
    for (const t of templates) {
      if (eventType && t.event_type !== eventType) continue;
      const val = t.key ?? t.id;
      if (!val || seen.has(val)) continue;
      seen.add(val);
      out.push({ value: val, label: val });
    }
    return out;
  }, [templates, eventType]);

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault();
    const recipientList = recipients.split(',').map((r) => r.trim()).filter(Boolean);
    if (!eventType.trim() || recipientList.length === 0) {
      setSendError('Enter an event type and at least one recipient');
      return;
    }
    setSending(true);
    setSendError(null);
    try {
      const body: NotificationSendRequest = {
        event_type: eventType.trim(),
        recipients: recipientList,
        channel,
        priority,
      };
      if (template.trim()) body.template = template.trim();
      await api.admin.notification.send(body, selectedTenantId ?? undefined);
      toast.success('Test notification submitted');
      setRecipients('');
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : 'Send failed');
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Notifications</h1>
        <p className="text-[12px] text-dim mt-0.5">Notification templates and test sends (→ notification)</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">Tenant</label>
        <CustomSelect
          options={[{ value: '', label: '(Default / current tenant)' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{error}</div>
      )}

      {/* Templates */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Notification templates</div>
        {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}
        {todo && (
          <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
            Not available yet: the notification template endpoint is unreachable. Templates will appear here once it's connected.
          </div>
        )}
        {!loading && !todo && templates.length === 0 && (
          <div className="font-mono text-[12px] text-dim italic">No templates</div>
        )}
        {!loading && !todo && templates.length > 0 && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Key</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Event type</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Locale</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Subject template</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                </tr>
              </thead>
              <tbody>
                {templates.map((t, i) => (
                  <tr key={t.id ?? t.key ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">{t.id ?? t.key ?? '—'}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{t.event_type ?? '—'}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{t.locale ?? '—'}</td>
                    <td
                      className="px-3 py-2 text-[12px] text-dim truncate max-w-[280px]"
                      title={t.subject_template ?? undefined}
                    >
                      {t.subject_template ?? '—'}
                    </td>
                    <td className="px-3 py-2 text-[12px]">
                      {t.enabled === undefined ? (
                        <span className="text-dim">—</span>
                      ) : t.enabled ? (
                        <span className="text-green-700">Enabled</span>
                      ) : (
                        <span className="text-dim">Disabled</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Send test notification */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Send test notification</div>
        <form onSubmit={handleSend} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-3 max-w-xl">
          {sendError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{sendError}</div>}
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Event type (event_type)</label>
              <ComboBox
                options={eventTypeOptions}
                value={eventType}
                onChange={setEventType}
                placeholder="user.welcome"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Channel (channel)</label>
              <CustomSelect
                options={[
                  { value: 'email', label: 'email' },
                  { value: 'sms', label: 'sms' },
                  { value: 'push', label: 'push' },
                  { value: 'webhook', label: 'webhook' },
                ]}
                value={channel}
                onChange={(v) => setChannel(v)}
              />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">Recipients (comma-separated)</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={recipients}
              onChange={(e) => setRecipients(e.target.value)}
              placeholder="user@acme.co, ops@acme.co"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Template (optional)</label>
              <ComboBox
                options={templateOptions}
                value={template}
                onChange={setTemplate}
                placeholder="welcome_v2"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Priority (priority)</label>
              <CustomSelect
                options={[
                  { value: 'low', label: 'low' },
                  { value: 'normal', label: 'normal' },
                  { value: 'high', label: 'high' },
                ]}
                value={priority}
                onChange={(v) => setPriority(v)}
              />
            </div>
          </div>
          <div>
            <button
              type="submit"
              disabled={sending}
              className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
            >
              {sending ? 'Sending…' : 'Send test notification'}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
