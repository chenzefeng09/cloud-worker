import { useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useApps } from '../hooks/useApps';
import { CreateAppModal } from '../components/app/CreateAppModal';
import { VersionStatusChip } from '../components/app/VersionStatusChip';
import { SchedulesPanel } from '../components/schedule/SchedulesPanel';
import type { App, AppVersion } from '../api/types';
import { personLabel } from '../lib/format';
import { useChatStore } from '../stores/chatStore';

/** Default glyph for an App card when the App has no icon set. */
const DEFAULT_APP_GLYPH = '▤';

type AppTab = 'mine' | 'published' | 'schedules';

/** Tab order is the nav order: own work first, then the shelf, then schedules. */
const TAB_ORDER: AppTab[] = ['mine', 'published', 'schedules'];

const TAB_LABELS: Record<AppTab, string> = {
  mine: 'My apps',
  published: 'App Center',
  schedules: 'Triggers',
};

/** `?tab=` is the source of truth so `/apps?tab=schedules` is linkable. */
function parseTab(raw: string | null): AppTab {
  return TAB_ORDER.includes(raw as AppTab) ? (raw as AppTab) : 'mine';
}

/** Pick the latest version (versions are returned newest-last) for status display. */
function latestVersion(app: App): AppVersion | null {
  const versions = app.versions ?? [];
  return versions.length ? versions[versions.length - 1] : null;
}

/** App card shared between the published shelf and the my-apps list. */
function AppCard({
  app,
  onClick,
  children,
}: {
  app: App;
  onClick: () => void;
  children?: React.ReactNode;
}) {
  const tags = app.tags ?? [];
  const me = useChatStore((s) => s.userProfile?.user_id);
  return (
    <div
      onClick={onClick}
      className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px"
    >
      <div className="flex items-center gap-2.5 mb-0.5">
        <div className="w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper">
          {app.icon || DEFAULT_APP_GLYPH}
        </div>
        <div className="text-[14px] font-semibold tracking-[-0.01em] truncate">{app.name}</div>
        <div className="flex-1" />
        <span className="font-mono text-[10.5px] text-dim">-&gt;</span>
      </div>
      <div className="text-[12px] text-dim leading-[1.45] line-clamp-2">
        {app.description || 'Run this app'}
      </div>
      <div className="flex-1" />
      <div className="flex items-center gap-1.5 flex-wrap">
        {children ?? <Chip>App</Chip>}
        {tags.map((t) => (
          <span key={t} className="cw-chip text-[9px] text-dim">#{t}</span>
        ))}
        <span className="font-mono text-[10.5px] text-dim ml-auto truncate" title={app.author_id}>
          {personLabel(app.author_id, app.author_name, me)}
        </span>
      </div>
    </div>
  );
}

export function AppListPage() {
  const navigate = useNavigate();
  const { apps: publishedApps, loading: pubLoading } = useApps('published');
  const { apps: myApps, loading: myLoading } = useApps('mine');
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseTab(searchParams.get('tab'));
  const [query, setQuery] = useState('');
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);

  const q = query.trim().toLowerCase();
  const currentApps = tab === 'published' ? publishedApps : myApps;
  const currentLoading = tab === 'published' ? pubLoading : myLoading;

  // All tags on the current tab (deduped + sorted for a stable chip order).
  const allTags = useMemo(() => {
    const set = new Set<string>();
    for (const a of currentApps) {
      for (const t of a.tags ?? []) set.add(t);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [currentApps]);

  // Search (name / description) AND tag OR-filter (app keeps if it carries any
  // of the selected tags). Both conditions compose on the current tab.
  const filteredApps = useMemo(() => {
    let list = currentApps;
    if (q) {
      list = list.filter(
        (a) =>
          a.name.toLowerCase().includes(q) ||
          (a.description ?? '').toLowerCase().includes(q),
      );
    }
    if (selectedTags.length > 0) {
      list = list.filter((a) => {
        const at = a.tags ?? [];
        return selectedTags.some((t) => at.includes(t));
      });
    }
    return list;
  }, [currentApps, q, selectedTags]);

  const switchTab = (t: AppTab) => {
    setSearchParams(t === 'mine' ? {} : { tab: t }, { replace: true });
    // Tag universe differs per tab; reset the selection so a stale tag does
    // not silently hide every app on the newly active tab.
    setSelectedTags([]);
  };

  const toggleTag = (tag: string) => {
    setSelectedTags((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag],
    );
  };

  const hasFilter = q.length > 0 || selectedTags.length > 0;

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: 'Home', to: '/home' }, { label: 'Apps' }]}
        actions={
          tab === 'schedules' ? null : (
            <button
              type="button"
              onClick={() => setShowCreateModal(true)}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              title="Create an app from scratch"
            >
              <span className="font-mono">+</span> Create app
            </button>
          )
        }
      />

      <div className="cw-scroll flex-1 min-h-0 overflow-auto px-[5vw] py-6">
        <div className="w-[min(960px,100%)] mx-auto flex flex-col gap-4">
          {/* Tab nav */}
          <div className="flex items-center gap-0.5 border-b border-line">
            {TAB_ORDER.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => switchTab(t)}
                className={`px-3 py-1.5 font-mono text-[11px] rounded-t-md border-b-2 -mb-px transition-colors ${
                  tab === t
                    ? 'border-accent text-accent bg-surface'
                    : 'border-transparent text-ink-2 hover:text-ink hover:bg-paper-2'
                }`}
              >
                {TAB_LABELS[t]}
              </button>
            ))}
          </div>

          {/* 触发器 tab reads as its own surface: no app search / tag filter,
              and creation lives on the app detail page's gear menu. */}
          {tab === 'schedules' ? (
            <SchedulesPanel />
          ) : (
            <>
              {/* Search */}
              <input
                className="border border-line rounded-sm px-2.5 py-1.5 text-[13px] bg-surface focus:outline-none focus:border-accent"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search apps by name or description…"
              />

              {/* Tag filter - only when the current tab has apps with tags */}
              {currentApps.length > 0 && allTags.length > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="cw-label mr-0.5">Tags</span>
                  {allTags.map((t) => {
                    const on = selectedTags.includes(t);
                    return (
                      <button
                        key={t}
                        type="button"
                        onClick={() => toggleTag(t)}
                        className="cw-chip"
                        data-accent={on ? '1' : undefined}
                        data-dashed={!on ? '1' : undefined}
                      >
                        #{t}
                      </button>
                    );
                  })}
                  {selectedTags.length > 0 && (
                    <button
                      type="button"
                      onClick={() => setSelectedTags([])}
                      className="font-mono text-[10.5px] text-dim hover:text-accent transition-colors ml-1"
                    >
                      Clear
                    </button>
                  )}
                </div>
              )}

              {/* List */}
              {currentLoading ? (
                <div className="font-mono text-[12px] text-dim">Loading…</div>
              ) : filteredApps.length === 0 ? (
                <div className="border border-line border-dashed bg-surface rounded-md p-4 flex flex-col gap-2 items-center justify-center min-h-[120px]">
                  <div className="font-mono italic text-dim text-[12px]">
                    {hasFilter
                      ? tab === 'published'
                        ? 'No matching published apps'
                        : 'No matching apps'
                      : tab === 'published'
                        ? 'No published apps yet'
                        : 'You haven’t created any apps yet'}
                  </div>
                  {!hasFilter && tab === 'mine' && (
                    <button
                      type="button"
                      onClick={() => setShowCreateModal(true)}
                      className="cw-btn cw-btn-ghost cw-btn-sm"
                    >
                      <span className="font-mono">+</span> Create app
                    </button>
                  )}
                </div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
                  {filteredApps.map((app) => {
                    if (tab === 'published') {
                      const canRun = Boolean(app.current_version_id);
                      return (
                        <AppCard key={app.id} app={app} onClick={() => navigate(`/apps/${app.id}`)}>
                          <Chip>App</Chip>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              navigate(`/apps/${app.id}/run`);
                            }}
                            disabled={!canRun}
                            title={canRun ? 'Run this app' : 'This app hasn’t been published yet'}
                            className="cw-btn cw-btn-primary cw-btn-sm ml-auto disabled:opacity-40 disabled:cursor-not-allowed"
                          >
                            Run
                          </button>
                        </AppCard>
                      );
                    }
                    const lv = latestVersion(app);
                    return (
                      <AppCard key={app.id} app={app} onClick={() => navigate(`/apps/${app.id}`)}>
                        {lv ? <VersionStatusChip status={lv.status} /> : <Chip>Draft</Chip>}
                      </AppCard>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <CreateAppModal open={showCreateModal} onClose={() => setShowCreateModal(false)} />
    </div>
  );
}
