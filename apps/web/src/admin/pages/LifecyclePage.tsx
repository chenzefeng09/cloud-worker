import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useChatStore } from '../../stores/chatStore';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type { ApprovalRequest, ApprovalStatus, LegalHold, RecycleBinItem } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

const OPERATION_LABEL: Record<ApprovalRequest['operation'], string> = {
  HARD_DELETE: 'Permanent deletion',
  RELEASE_LEGAL_HOLD: 'Legal hold release',
};

const STATUS_FILTERS: { value: ApprovalStatus | ''; label: string }[] = [
  { value: 'PENDING', label: 'Pending' },
  { value: 'APPROVED', label: 'Approved' },
  { value: 'REJECTED', label: 'Rejected' },
  { value: 'EXPIRED', label: 'Expired' },
  { value: '', label: 'All' },
];

// The lifecycle service's four-eyes refusals in plain words. Its codes read
// FOUNDATION.LIFECYCLE.<category>.<name>; any other error shows its own message.
const REFUSALS: Record<string, string> = {
  SELF_APPROVAL_FORBIDDEN: 'You opened this request, so another admin has to approve it.',
  APPROVAL_NOT_PENDING: 'This request was already decided or has expired.',
  LEGAL_HOLD_ACTIVE: 'A legal hold covers this resource, so it can’t be permanently deleted.',
  RESOURCE_NOT_IN_BIN: 'This item is no longer in the recycle bin: it was restored or already deleted.',
  LEGAL_HOLD_NOT_FOUND: 'This legal hold no longer exists.',
  APPROVAL_REQUEST_NOT_FOUND: 'This approval request no longer exists.',
};

const refusalName = (err: unknown) => (err instanceof ApiError ? err.code.split('.').pop() : undefined);

function describeError(err: unknown, fallback: string): string {
  const plain = REFUSALS[refusalName(err) ?? ''];
  if (plain) return plain;
  return err instanceof ApiError ? err.message : fallback;
}

export function LifecyclePage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const currentUserId = useChatStore((s) => s.userProfile?.user_id);
  const toast = useToast();
  const confirm = useConfirm();
  const tid = selectedTenantId ?? undefined;

  const [items, setItems] = useState<RecycleBinItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [holds, setHolds] = useState<LegalHold[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // What the last hard-delete / release / approval decision came to. It stays
  // up (unlike a toast) because it carries the request's expiry or a refusal.
  const [notice, setNotice] = useState<{ tone: 'info' | 'error'; text: string } | null>(null);

  // Four-eyes approval requests.
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [approvalsCursor, setApprovalsCursor] = useState<string | null>(null);
  const [approvalStatus, setApprovalStatus] = useState<ApprovalStatus | ''>('PENDING');
  const [approvalsLoading, setApprovalsLoading] = useState(false);
  const [approvalsTodo, setApprovalsTodo] = useState(false);
  const [approvalsError, setApprovalsError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState<string | null>(null);
  // Only the latest list request may land (filter / tenant switches race).
  const approvalsSeq = useRef(0);

  // Soft-delete form.
  const [sdOpen, setSdOpen] = useState(false);
  const [sdTypeId, setSdTypeId] = useState('');
  const [sdResId, setSdResId] = useState('');
  const [sdName, setSdName] = useState('');
  const [sdBusy, setSdBusy] = useState(false);

  // Legal-hold form.
  const [lhOpen, setLhOpen] = useState(false);
  const [lhTypeId, setLhTypeId] = useState('');
  const [lhResId, setLhResId] = useState('');
  const [lhReason, setLhReason] = useState('');
  const [lhCase, setLhCase] = useState('');
  const [lhBusy, setLhBusy] = useState(false);
  const [lhError, setLhError] = useState<string | null>(null);

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
    Promise.all([
      api.admin.lifecycle.recycleBin.list({ limit: 50 }, tid),
      api.admin.lifecycle.legalHolds.list(tid),
    ])
      .then(([page, h]) => {
        setItems(page.items ?? []);
        setNextCursor(page.next_cursor ?? null);
        setHolds(h);
      })
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : 'Failed to load'); })
      .finally(() => setLoading(false));
  }, [tid]);

  useEffect(() => { load(); }, [load, reloadKey]);

  // Loaded apart from the recycle bin and holds: a lifecycle service from
  // before four-eyes approval has no approval requests (404), and that must
  // not hide the rest of the page.
  const loadApprovals = useCallback((cursor?: string) => {
    const seq = ++approvalsSeq.current;
    setApprovalsLoading(true);
    setApprovalsError(null);
    setApprovalsTodo(false);
    api.admin.lifecycle.approvalRequests
      .list({ status: approvalStatus || undefined, cursor, limit: 50 }, tid)
      .then((page) => {
        if (seq !== approvalsSeq.current) return;
        const rows = page.items ?? [];
        setApprovals((prev) => (cursor ? [...prev, ...rows] : rows));
        setApprovalsCursor(page.next_cursor ?? null);
      })
      .catch((err) => {
        if (seq !== approvalsSeq.current) return;
        if (!cursor) { setApprovals([]); setApprovalsCursor(null); }
        if (isTodo(err)) setApprovalsTodo(true);
        else setApprovalsError(err instanceof ApiError ? err.message : 'Failed to load approval requests');
      })
      .finally(() => { if (seq === approvalsSeq.current) setApprovalsLoading(false); });
  }, [tid, approvalStatus]);

  useEffect(() => { loadApprovals(); }, [loadApprovals, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const isOwn = (a: ApprovalRequest) => !!currentUserId && a.requested_by === currentUserId;

  // What a request is about, in the terms of the tables below, when the entry
  // or hold is on this page.
  const describeTarget = (a: ApprovalRequest): string | null => {
    const subject = a.operation === 'HARD_DELETE'
      ? items.find((it) => it.id === a.target_id)
      : holds.find((h) => h.id === a.target_id);
    if (!subject?.resource_type && !subject?.resource_id) return null;
    return `${subject.resource_type ?? '—'} · ${subject.resource_id ?? '—'}`;
  };

  // A 202 from hard-delete / release: nothing happened yet. The request may be
  // one another admin opened earlier (the service returns the pending one).
  const awaitingApproval = (what: string, req: ApprovalRequest): string => {
    const expiry = `The request expires at ${fmt(req.expires_at)}.`;
    if (currentUserId && req.requested_by !== currentUserId) {
      return `${what} was already requested by ${req.requested_by} and is waiting for a second admin to approve it, which you can do under Approval requests. ${expiry}`;
    }
    return `${what} requested. It is waiting for a second admin to approve it. ${expiry}`;
  };

  const handleRestore = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: 'Restore resource', message: 'Restore this resource from the recycle bin?', confirmText: 'Restore' }))) return;
    try {
      await api.admin.lifecycle.recycleBin.restore(id, tid);
      toast.success('Restored');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Restore failed');
    }
  };

  const handleHardDelete = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({
      title: 'Permanently delete',
      message: 'Permanent deletion needs two admins: this opens a request that another admin has to approve before it expires. Once approved, the item cannot be recovered. Continue?',
      tone: 'danger',
      confirmText: 'Request deletion',
    }))) return;
    setNotice(null);
    try {
      const res = await api.admin.lifecycle.recycleBin.hardDelete(id, tid);
      if (res.destroyed) toast.info('Already permanently deleted');
      else setNotice({ tone: 'info', text: awaitingApproval('Permanent deletion', res.approval_request) });
      refresh();
    } catch (err) {
      setNotice({ tone: 'error', text: describeError(err, 'Delete request failed') });
    }
  };

  const handleSweep = async () => {
    if (!(await confirm({ title: 'Purge expired', message: 'This permanently destroys every expired item in the recycle bin. Continue?', tone: 'danger', confirmText: 'Purge' }))) return;
    try {
      const res = await api.admin.lifecycle.sweep(tid);
      const n = typeof res?.destroyed === 'number' ? res.destroyed : 0;
      toast.success(`Purged ${n} item(s)`);
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Purge failed');
    }
  };

  const handleSoftDelete = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!sdTypeId.trim() || !sdResId.trim()) { setError('Enter a resource type ID and resource ID'); return; }
    setSdBusy(true);
    setError(null);
    try {
      await api.admin.lifecycle.recycleBin.softDelete(
        { resource_type_id: sdTypeId.trim(), resource_id: sdResId.trim(), resource_name: sdName.trim() || null },
        tid,
      );
      toast.success('Soft-deleted');
      setSdTypeId(''); setSdResId(''); setSdName(''); setSdOpen(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Soft delete failed');
    } finally {
      setSdBusy(false);
    }
  };

  const handleCreateHold = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!lhTypeId.trim() || !lhResId.trim()) { setLhError('Enter a resource type ID and resource ID'); return; }
    if (lhReason.trim().length < 10) { setLhError('The hold reason must be at least 10 characters'); return; }
    setLhBusy(true);
    setLhError(null);
    try {
      await api.admin.lifecycle.legalHolds.create(
        {
          resource_type_id: lhTypeId.trim(),
          resource_id: lhResId.trim(),
          reason: lhReason.trim(),
          case_reference: lhCase.trim() || null,
        },
        tid,
      );
      toast.success('Legal hold placed');
      setLhTypeId(''); setLhResId(''); setLhReason(''); setLhCase(''); setLhOpen(false);
      refresh();
    } catch (err) {
      setLhError(err instanceof ApiError ? err.message : 'Create failed');
    } finally {
      setLhBusy(false);
    }
  };

  const handleReleaseHold = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({
      title: 'Release legal hold',
      message: 'Releasing a legal hold needs two admins: this opens a request that another admin has to approve before it expires. Continue?',
      tone: 'danger',
      confirmText: 'Request release',
    }))) return;
    setNotice(null);
    try {
      const res = await api.admin.lifecycle.legalHolds.release(id, tid);
      if (res.released) toast.info('Already released');
      else setNotice({ tone: 'info', text: awaitingApproval('Legal hold release', res.approval_request) });
      refresh();
    } catch (err) {
      setNotice({ tone: 'error', text: describeError(err, 'Release request failed') });
    }
  };

  const handleApprove = async (a: ApprovalRequest) => {
    const subject = describeTarget(a) ?? a.target_id;
    const hardDelete = a.operation === 'HARD_DELETE';
    if (!(await confirm({
      title: hardDelete ? 'Approve permanent deletion' : 'Approve legal hold release',
      message: hardDelete
        ? `Approving permanently deletes ${subject} now, with you recorded as the witness. It cannot be recovered. Continue?`
        : `Approving releases the legal hold on ${subject} now. Continue?`,
      tone: 'danger',
      confirmText: 'Approve',
    }))) return;
    setNotice(null);
    setDeciding(a.id);
    try {
      await api.admin.lifecycle.approvalRequests.approve(a.id, tid);
      toast.success(hardDelete ? 'Approved: permanently deleted' : 'Approved: legal hold released');
    } catch (err) {
      // The deletion's own checks run again on approval; when they refuse,
      // the request is left pending.
      const name = refusalName(err);
      const stillPending = name === 'LEGAL_HOLD_ACTIVE' || name === 'RESOURCE_NOT_IN_BIN';
      setNotice({
        tone: 'error',
        text: describeError(err, 'Approval failed') + (stillPending ? ' The request stays pending.' : ''),
      });
    } finally {
      setDeciding(null);
      refresh();
    }
  };

  const handleReject = async (a: ApprovalRequest) => {
    if (!(await confirm({
      title: isOwn(a) ? 'Withdraw request' : 'Reject request',
      message: `Reject this ${OPERATION_LABEL[a.operation].toLowerCase()} request? Nothing is deleted or released.`,
      confirmText: 'Reject',
    }))) return;
    setNotice(null);
    setDeciding(a.id);
    try {
      await api.admin.lifecycle.approvalRequests.reject(a.id, tid);
      toast.success('Request rejected');
    } catch (err) {
      setNotice({ tone: 'error', text: describeError(err, 'Reject failed') });
    } finally {
      setDeciding(null);
      refresh();
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Data lifecycle</h1>
        <p className="text-[12px] text-dim mt-0.5">Recycle bin, expiry purge, legal holds and their approvals (→ lifecycle)</p>
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

      {error && <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{error}</div>}
      {notice && (
        <div
          role={notice.tone === 'error' ? 'alert' : 'status'}
          className={`flex items-start justify-between gap-3 rounded-md px-4 py-3 text-[13px] border ${
            notice.tone === 'error' ? 'border-accent-line bg-accent-soft text-accent' : 'border-line bg-surface text-ink-2'
          }`}
        >
          <span>{notice.text}</span>
          <button onClick={() => setNotice(null)} aria-label="Dismiss" className="text-[12px] leading-none text-dim hover:text-ink">×</button>
        </div>
      )}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">Not available yet: the data lifecycle endpoint is unreachable. This section appears automatically once it's wired up.</div>}
      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}

      {/* Four-eyes approval requests */}
      {!todo && (
        <section className="flex flex-col gap-2" aria-label="Approval requests">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Approval requests</div>
            <div className="flex items-center gap-2">
              <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">Status</label>
              <CustomSelect
                options={STATUS_FILTERS}
                value={approvalStatus}
                onChange={(v) => setApprovalStatus(v as ApprovalStatus | '')}
                className="min-w-[120px]"
              />
            </div>
          </div>
          <div className="text-[11px] text-dim">Permanent deletion and legal hold release need two admins: each takes effect only when an admin other than the one who asked approves it before it expires. Anyone can reject a request, including the admin who asked.</div>
          {approvalsTodo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">Not available yet: this lifecycle service has no approval requests.</div>}
          {approvalsError && <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{approvalsError}</div>}
          {!approvalsLoading && !approvalsTodo && !approvalsError && approvals.length === 0 && (
            <div className="font-mono text-[12px] text-dim italic">
              {approvalStatus ? `No ${approvalStatus.toLowerCase()} approval requests` : 'No approval requests'}
            </div>
          )}
          {approvals.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Operation</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Target</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Requested by</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Expires at</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                    <th className="px-3 py-2 w-40"></th>
                  </tr>
                </thead>
                <tbody>
                  {approvals.map((a) => {
                    const own = isOwn(a);
                    const target = describeTarget(a);
                    return (
                      <tr key={a.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 text-[12px] text-ink-2">{OPERATION_LABEL[a.operation] ?? a.operation}</td>
                        <td className="px-3 py-2 font-mono text-[11px] max-w-[200px]">
                          <div className="text-ink-2 truncate">{target ?? a.target_id}</div>
                          {target && <div className="text-[10.5px] text-dim truncate">{a.target_id}</div>}
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim max-w-[180px]">
                          <div className="truncate">{a.requested_by}{own && <span className="text-ink-2"> (you)</span>}</div>
                          <div className="text-[10.5px]">{fmt(a.requested_at)}</div>
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(a.expires_at)}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                          <div>{a.status}</div>
                          {a.decided_by && <div className="text-[10.5px] text-dim truncate max-w-[180px]">by {a.decided_by} · {fmt(a.decided_at)}</div>}
                        </td>
                        <td className="px-3 py-2">
                          {a.status === 'PENDING' && (
                            <>
                              <div className="flex items-center gap-2">
                                <button
                                  onClick={() => handleApprove(a)}
                                  disabled={own || deciding === a.id}
                                  title={own ? REFUSALS.SELF_APPROVAL_FORBIDDEN : undefined}
                                  className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent"
                                >
                                  Approve
                                </button>
                                <button
                                  onClick={() => handleReject(a)}
                                  disabled={deciding === a.id}
                                  className="text-[11px] font-mono text-ink-2 border border-line rounded-sm px-2 py-0.5 hover:bg-paper-2 disabled:opacity-40 disabled:cursor-not-allowed"
                                >
                                  Reject
                                </button>
                              </div>
                              {own && <div className="mt-1 text-[10.5px] text-dim">Another admin has to approve</div>}
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {approvalsCursor && (
            <button
              onClick={() => loadApprovals(approvalsCursor)}
              disabled={approvalsLoading}
              className="self-start px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
            >
              Load more
            </button>
          )}
        </section>
      )}

      {/* Recycle bin */}
      {!todo && (
        <section className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Recycle bin</div>
            <div className="flex items-center gap-2">
              {!sdOpen && <button onClick={() => setSdOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ Soft delete</button>}
              <button onClick={handleSweep} className="px-2.5 py-1 text-[11px] font-mono border border-red-200 text-red-600 rounded-sm hover:bg-red-50">Purge expired</button>
            </div>
          </div>

          {sdOpen && (
            <Dialog title="Manual soft delete" onClose={() => setSdOpen(false)} dismissable={!sdBusy} className="w-full max-w-2xl">
            <form onSubmit={handleSoftDelete} className="flex flex-col gap-2">
              <div className="text-[11px] text-dim">The resource type ID must be a registered type (e.g. <code className="font-mono">rt_…</code>); otherwise the downstream service returns “resource type not registered”.</div>
              <div className="grid grid-cols-3 gap-2">
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">Resource type ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={sdTypeId} onChange={(e) => setSdTypeId(e.target.value)} placeholder="rt_document00000000000000001" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">Resource ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={sdResId} onChange={(e) => setSdResId(e.target.value)} placeholder="doc-123" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">Resource name (optional)</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={sdName} onChange={(e) => setSdName(e.target.value)} />
                </div>
              </div>
              <div className="flex gap-2">
                <button type="submit" disabled={sdBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{sdBusy ? 'Submitting…' : 'Soft delete'}</button>
                <button type="button" onClick={() => setSdOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
              </div>
            </form>
            </Dialog>
          )}

          {!loading && !todo && items.length === 0 && <div className="font-mono text-[12px] text-dim italic">The recycle bin is empty</div>}
          {items.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Resource type</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Resource ID</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Deleted at</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Expires at</th>
                    <th className="px-3 py-2 w-40"></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((it, i) => (
                    <tr key={it.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                      <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{it.resource_type ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">{it.resource_id ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(it.deleted_at)}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(it.expires_at)}</td>
                      <td className="px-3 py-2 flex items-center gap-2">
                        <button onClick={() => handleRestore(it.id)} className="text-[11px] font-mono text-green-700 border border-green-200 rounded-sm px-2 py-0.5 hover:bg-green-50">Restore</button>
                        <button onClick={() => handleHardDelete(it.id)} className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50">Permanently delete</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {nextCursor && <div className="font-mono text-[10.5px] text-dim">More available (cursor: {nextCursor})</div>}
        </section>
      )}

      {/* Legal holds */}
      {!todo && (
        <section className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Legal holds</div>
            {!lhOpen && <button onClick={() => setLhOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ New hold</button>}
          </div>

          {lhOpen && (
            <Dialog title="New legal hold" onClose={() => setLhOpen(false)} dismissable={!lhBusy} className="w-full max-w-2xl">
            <form onSubmit={handleCreateHold} className="flex flex-col gap-2">
              {lhError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{lhError}</div>}
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">Resource type ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={lhTypeId} onChange={(e) => setLhTypeId(e.target.value)} placeholder="rt_document00000000000000001" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">Resource ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={lhResId} onChange={(e) => setLhResId(e.target.value)} placeholder="doc-123" />
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Hold reason (≥10 characters)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={lhReason} onChange={(e) => setLhReason(e.target.value)} placeholder="Preserve data for litigation" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Case number (optional)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={lhCase} onChange={(e) => setLhCase(e.target.value)} />
              </div>
              <div className="flex gap-2">
                <button type="submit" disabled={lhBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{lhBusy ? 'Creating…' : 'Create'}</button>
                <button type="button" onClick={() => setLhOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
              </div>
            </form>
            </Dialog>
          )}

          {!loading && !todo && holds.length === 0 && <div className="font-mono text-[12px] text-dim italic">No legal holds</div>}
          {holds.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Resource type</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Resource ID</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Reason</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Placed at</th>
                    <th className="px-3 py-2 w-20"></th>
                  </tr>
                </thead>
                <tbody>
                  {holds.map((h, i) => (
                    <tr key={h.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                      <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{h.resource_type ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">{h.resource_id ?? '—'}</td>
                      <td className="px-3 py-2 text-[12px] text-ink-2 truncate max-w-[220px]">{h.reason ?? '—'}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(h.placed_at)}</td>
                      <td className="px-3 py-2">
                        <button onClick={() => handleReleaseHold(h.id)} className="text-[11px] font-mono text-accent hover:opacity-80">Release</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
