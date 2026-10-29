import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { ActionMenu, type ActionMenuItem } from '../components/shell/ActionMenu';
import { api, ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';
import { useConfirm } from '../admin/components/dialogs';
import { VersionStatusChip } from '../components/app/VersionStatusChip';
import { RunCard } from '../components/app/RunCard';
import { ScheduleCreateDialog } from '../components/schedule/ScheduleCreateDialog';
import { VersionHistoryDialog } from '../components/app/VersionHistoryDialog';
import type {
  App,
  AppRunHistoryItem,
  AppVersion,
  AppVisibility,
} from '../api/types';

/** How many recent runs the detail page previews before linking out. */
const RECENT_RUNS_LIMIT = 5;

/** Default glyph shown when the App has no icon set. */
const DEFAULT_APP_GLYPH = '▤';

const VISIBILITY_LABEL: Record<AppVisibility, string> = {
  private: 'Private',
  tenant: 'Tenant',
};

/** Resolve the published version of an App (the one current_version_id points at). */
function resolvePublishedVersion(app: App): AppVersion | null {
  const versions = app.versions ?? [];
  if (app.current_version_id) {
    const v = versions.find((x) => x.id === app.current_version_id);
    if (v) return v;
  }
  return versions.find((x) => x.status === 'published') ?? null;
}

/** Pick the latest version (newest-last) for the header status chip. */
function latestVersion(app: App): AppVersion | null {
  const versions = app.versions ?? [];
  return versions.length ? versions[versions.length - 1] : null;
}

// ─── Section wrapper (matches AppEditorPage / AppReviewPage convention) ─────────

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div>
        <h2 className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">{title}</h2>
        {hint && <p className="text-[11.5px] text-dim mt-1">{hint}</p>}
      </div>
      {children}
    </section>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function AppDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const userProfile = useChatStore((s) => s.userProfile);
  const confirm = useConfirm();

  const [app, setApp] = useState<App | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [versionHistoryOpen, setVersionHistoryOpen] = useState(false);

  // Recent-runs preview. Fetched once — unlike AppHistoryPage this surface
  // doesn't poll non-terminal runs; the full history page owns that job.
  const [runs, setRuns] = useState<AppRunHistoryItem[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) {
      navigate('/home', { replace: true });
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.apps
      .get(id)
      .then((a) => { if (!cancelled) setApp(a); })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load app');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [id, navigate]);

  // Runs load independently of the app so a history failure degrades to an
  // in-card message instead of blanking the whole page.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setRunsLoading(true);
    setRunsError(null);
    api.apps
      .listRuns(id, { limit: RECENT_RUNS_LIMIT, offset: 0 })
      .then((resp) => { if (!cancelled) setRuns(resp.items); })
      .catch((err) => {
        if (!cancelled) setRunsError(err instanceof ApiError ? err.message : 'Failed to load run history');
      })
      .finally(() => { if (!cancelled) setRunsLoading(false); });
    return () => { cancelled = true; };
  }, [id]);

  const published = app ? resolvePublishedVersion(app) : null;
  const latest = app ? latestVersion(app) : null;
  const canRun = Boolean(published);
  // 定时运行 always fires the published harness, so an app without one has
  // nothing to schedule.
  const canSchedule = (app?.versions ?? []).some((v) => v.status === 'published');
  const isOwner = Boolean(app && userProfile && app.author_id === userProfile.id);
  const isAdmin = Boolean(userProfile?.roles?.includes('super_admin'));
  const canManage = isOwner || isAdmin;

  const handleDelete = async () => {
    if (!app) return;
    const ok = await confirm({
      title: 'Delete app',
      message: `Delete the app “${app.name}”? This can’t be undone — all versions and run history will be removed.`,
      tone: 'danger',
      confirmText: 'Delete',
    });
    if (!ok) return;
    setDeleting(true);
    try {
      await api.apps.remove(app.id);
      navigate('/apps', { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  const crumbs = [
    { label: 'Home', to: '/home' },
    { label: 'Apps', to: '/apps' },
    { label: app?.name || 'App' },
  ];

  // Secondary/destructive commands live behind the page-head ⋯ so the primary
  // 运行应用 CTA in the hero has no competition.
  const menuItems: ActionMenuItem[] =
    app && canManage
      ? [
          {
            id: 'edit',
            label: 'Edit app',
            disabled: deleting,
            onSelect: () => navigate(`/apps/${app.id}/edit`),
          },
          {
            id: 'schedule',
            label: 'Set up trigger',
            disabled: deleting || !canSchedule,
            title: canSchedule ? undefined : 'Publish the app before setting up a trigger',
            onSelect: () => setScheduleOpen(true),
          },
          { id: 'delete', label: 'Delete', danger: true, disabled: deleting, onSelect: handleDelete },
        ]
      : [];

  if (loading) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex items-center justify-center font-mono text-[12px] text-dim">
          Loading…
        </div>
      </div>
    );
  }

  if (error || !app) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex flex-col items-center justify-center gap-2 text-dim">
          <div className="text-[14px] text-ink font-medium">Failed to load app</div>
          <div className="text-[12.5px]">{error || 'App not found.'}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={crumbs}
        actions={menuItems.length > 0 ? <ActionMenu items={menuItems} icon="gear" /> : null}
      />

      <div className="cw-scroll flex-1 min-h-0 overflow-auto px-[5vw] py-6">
        <div className="w-[min(880px,100%)] mx-auto flex flex-col gap-4">
          {/* Hero — identity on the left, the primary 运行应用 CTA on the right so
              the main action is in the first screenful rather than buried in the
              page head. Wraps on narrow viewports instead of overflowing. */}
          <div className="border border-line rounded-md bg-surface p-4 sm:p-5 flex flex-wrap items-start gap-4">
            <div className="flex items-start gap-3 min-w-[240px] flex-1">
              <div className="w-12 h-12 border border-line rounded-md flex items-center justify-center font-mono text-[22px] text-ink bg-paper shrink-0">
                {app.icon || DEFAULT_APP_GLYPH}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <h1 className="m-0 font-display font-normal tracking-[-0.01em] text-[22px] leading-[1.2]">
                    {app.name}
                  </h1>
                  <Chip>{VISIBILITY_LABEL[app.visibility]}</Chip>
                  {latest && <VersionStatusChip status={latest.status} />}
                </div>
                {app.description && (
                  <p className="mt-1 mb-0 text-[13px] text-dim leading-[1.5]">{app.description}</p>
                )}
                <div className="mt-1 font-mono text-[10.5px] text-dim">
                  Author {app.author_id}
                  {published && <span className="ml-2">· Current version v{published.version}</span>}
                </div>
              </div>
            </div>
            <div className="flex flex-col items-stretch gap-1 shrink-0">
              <button
                type="button"
                onClick={() => navigate(`/apps/${app.id}/run`)}
                disabled={!canRun}
                aria-describedby={canRun ? undefined : 'app-run-disabled-hint'}
                className="cw-btn cw-btn-primary px-6 py-1.5 text-[13px] justify-center disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Run
              </button>
              {!canRun && (
                <p id="app-run-disabled-hint" className="m-0 text-[11.5px] text-dim text-center">
                  This app hasn’t been published yet
                </p>
              )}
              {/* 版本历史 entry is visible to all users (not just author/admin);
                  the list moved off the page into this dialog. */}
              <button
                type="button"
                onClick={() => setVersionHistoryOpen(true)}
                className="cw-btn cw-btn-ghost cw-btn-sm justify-center"
              >
                Version history
              </button>
            </div>
          </div>

          {/* Execution history — most recent runs inline, full list one click away */}
          <Section title="Run history" hint={`The last ${RECENT_RUNS_LIMIT} runs.`}>
            {runsError && (
              <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
                {runsError}
              </div>
            )}

            {runsLoading && !runsError && (
              <div className="font-mono text-[12px] text-dim">Loading…</div>
            )}

            {!runsLoading && !runsError && runs.length === 0 && (
              <div className="font-mono italic text-dim text-[12px]">No runs yet.</div>
            )}

            {!runsLoading && runs.length > 0 && (
              <div className="flex flex-col gap-2.5">
                {runs.map((run) => (
                  <RunCard
                    key={run.id}
                    run={run}
                    onOpen={(r) => navigate(`/apps/runs/${r.session_id}/${r.id}`)}
                  />
                ))}
              </div>
            )}

            {/* A full page means there may be more behind it — only then link out. */}
            {!runsLoading && runs.length >= RECENT_RUNS_LIMIT && (
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={() => navigate(`/apps/${app.id}/history`)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                >
                  View more
                </button>
              </div>
            )}
          </Section>
        </div>
      </div>

      {/* 设置触发器 — the App is fixed, so the dialog has no app picker.
          After creation, land on the 触发器 tab where the trigger now shows. */}
      {scheduleOpen && (
        <ScheduleCreateDialog
          app={app}
          onClose={() => setScheduleOpen(false)}
          onCreated={() => { setScheduleOpen(false); navigate('/apps?tab=schedules'); }}
        />
      )}

      {/* 版本历史 - list moved off the page into this dialog; entry in the hero. */}
      <VersionHistoryDialog
        app={app}
        open={versionHistoryOpen}
        onClose={() => setVersionHistoryOpen(false)}
      />
    </div>
  );
}
