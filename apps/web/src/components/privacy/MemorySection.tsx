import { useCallback, useEffect, useState } from 'react';
import { privacyApi, type MemoryItem, type PrivacySettings } from '../../api/privacy';
import { useConfirm } from '../../admin/components/dialogs';
import { formatRelativeTime } from '../../lib/format';
import { pluralize } from './labels';
import { AppLink, btn, describeError, InlineAlert, PrivacySection, PrivacySwitch, Prose } from './ui';

const LONG_CONTENT = 220;

function MemoryRow({
  memory,
  deleting,
  onDelete,
}: {
  memory: MemoryItem;
  deleting: boolean;
  onDelete: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const long = (memory.content ?? '').length > LONG_CONTENT;
  return (
    <li className="border border-line-2 rounded-sm bg-paper px-3 py-2.5 flex flex-col gap-1">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0 text-[13px] font-medium text-ink tracking-[-0.01em] break-words">
          {memory.title || 'Untitled memory'}
        </div>
        <button
          type="button"
          className={btn.ghost}
          onClick={onDelete}
          disabled={deleting}
          aria-label={`Delete memory: ${memory.title || 'Untitled memory'}`}
        >
          {deleting ? 'Deleting…' : 'Delete'}
        </button>
      </div>
      {memory.content && (
        <p className={`text-[12.5px] leading-[1.55] text-ink-2 whitespace-pre-line break-words ${long && !expanded ? 'line-clamp-3' : ''}`}>
          {memory.content}
        </p>
      )}
      <div className="flex items-center gap-2 font-mono text-[10.5px] text-dim">
        <span>Saved {formatRelativeTime(memory.created_at)}</span>
        {memory.origin_session_id && (
          <>
            <span aria-hidden>·</span>
            <AppLink to={`/tasks/${encodeURIComponent(memory.origin_session_id)}`}>From this task</AppLink>
          </>
        )}
        {long && (
          <>
            <span aria-hidden>·</span>
            <button type="button" className="underline underline-offset-2 hover:text-ink" onClick={() => setExpanded((v) => !v)}>
              {expanded ? 'Show less' : 'Show more'}
            </button>
          </>
        )}
      </div>
    </li>
  );
}

/**
 * Memory: the on/off preference (`/me/privacy/settings`) and the list of saved
 * memories with per-item and bulk delete.
 *
 * @param platformMemoryEnabled from the privacy notice; `false` means memory is
 *   switched off for everyone, so the personal toggle has no effect.
 */
export function MemorySection({ platformMemoryEnabled }: { platformMemoryEnabled?: boolean }) {
  const confirm = useConfirm();

  const [settings, setSettings] = useState<PrivacySettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [savingToggle, setSavingToggle] = useState(false);

  const [memories, setMemories] = useState<MemoryItem[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deletingAll, setDeletingAll] = useState(false);

  useEffect(() => {
    let cancelled = false;
    privacyApi
      .settings()
      .then((s) => { if (!cancelled) setSettings(s); })
      .catch((err) => { if (!cancelled) setSettingsError(describeError(err, "Couldn't load your memory setting.")); });
    return () => { cancelled = true; };
  }, []);

  const loadMemories = useCallback(() => {
    setListError(null);
    setMemories(null);
    privacyApi
      .memories()
      .then(setMemories)
      .catch((err) => setListError(describeError(err, "Couldn't load your memories.")));
  }, []);

  useEffect(() => { loadMemories(); }, [loadMemories]);

  const platformOff = platformMemoryEnabled === false;
  const on = !!settings?.memory_enabled;

  const handleToggle = async (next: boolean) => {
    setSavingToggle(true);
    setSettingsError(null);
    try {
      setSettings(await privacyApi.updateSettings({ memory_enabled: next }));
    } catch (err) {
      setSettingsError(describeError(err, "Couldn't save your memory setting. Please try again."));
    } finally {
      setSavingToggle(false);
    }
  };

  const handleDelete = async (memory: MemoryItem) => {
    setDeletingId(memory.id);
    setActionError(null);
    setNotice(null);
    try {
      await privacyApi.deleteMemory(memory.id);
      setMemories((prev) => (prev ?? []).filter((m) => m.id !== memory.id));
      setNotice('Memory deleted.');
    } catch (err) {
      setActionError(describeError(err, "Couldn't delete that memory. Please try again."));
    } finally {
      setDeletingId(null);
    }
  };

  const handleDeleteAll = async () => {
    const count = memories?.length ?? 0;
    const ok = await confirm({
      title: 'Delete all memories',
      message: `Delete ${count === 1 ? 'your saved memory' : `all ${count.toLocaleString()} saved memories`}? Later tasks won't be able to use ${count === 1 ? 'it' : 'them'}. This can't be undone.`,
      confirmText: 'Delete all',
      tone: 'danger',
    });
    if (!ok) return;
    setDeletingAll(true);
    setActionError(null);
    setNotice(null);
    try {
      const res = await privacyApi.deleteAllMemories();
      setMemories([]);
      setNotice(`Deleted ${pluralize(res?.deleted ?? count, 'memory', 'memories')}.`);
    } catch (err) {
      setActionError(describeError(err, "Couldn't delete your memories. Please try again."));
      loadMemories(); // some may be gone — show what's left
    } finally {
      setDeletingAll(false);
    }
  };

  return (
    <PrivacySection
      title="Memory"
      action={
        <button
          type="button"
          className={btn.ghost}
          onClick={handleDeleteAll}
          disabled={deletingAll || !memories || memories.length === 0}
        >
          {deletingAll ? 'Deleting…' : 'Delete all'}
        </button>
      }
    >
      <Prose>
        Memory saves a short summary of each task you complete and reuses it as context in your later tasks, so
        you don't have to repeat yourself.
      </Prose>

      <div className="flex items-center gap-4">
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-medium tracking-[-0.01em] text-ink">Use memory</div>
          <div className="font-mono text-[10.5px] text-dim">
            {settings === null && !settingsError
              ? 'Loading…'
              : savingToggle
                ? 'Saving…'
                : settings?.updated_at
                  ? `${on ? 'On' : 'Off'} · changed ${formatRelativeTime(settings.updated_at)}`
                  : on ? 'On' : 'Off'}
          </div>
        </div>
        <PrivacySwitch
          label="Use memory"
          checked={on}
          onChange={handleToggle}
          disabled={settings === null || savingToggle || platformOff}
        />
      </div>
      {platformOff && (
        <InlineAlert tone="info">Memory is switched off for this workspace by your administrator, so nothing is saved right now.</InlineAlert>
      )}
      {settings && !on && !platformOff && (
        <InlineAlert tone="info">
          Memory is off: nothing new is saved and saved memories aren't used in new tasks. Memories saved earlier
          stay below until you delete them.
        </InlineAlert>
      )}
      {settingsError && <InlineAlert tone="error">{settingsError}</InlineAlert>}

      <div className="cw-divider" />

      <div className="flex items-center gap-2">
        <span className="cw-label">Saved memories{memories ? ` (${memories.length})` : ''}</span>
      </div>
      {notice && <InlineAlert tone="success">{notice}</InlineAlert>}
      {actionError && <InlineAlert tone="error">{actionError}</InlineAlert>}
      {listError ? (
        <div className="flex items-center gap-3">
          <InlineAlert tone="error">{listError}</InlineAlert>
          <button type="button" className={btn.base} onClick={loadMemories}>Retry</button>
        </div>
      ) : memories === null ? (
        <div className="font-mono text-[11px] text-dim">Loading…</div>
      ) : memories.length === 0 ? (
        <div className="font-mono text-[11px] text-dim italic">No memories saved yet.</div>
      ) : (
        <ul className="flex flex-col gap-2" aria-label="Saved memories">
          {memories.map((m) => (
            <MemoryRow key={m.id} memory={m} deleting={deletingId === m.id || deletingAll} onDelete={() => handleDelete(m)} />
          ))}
        </ul>
      )}
    </PrivacySection>
  );
}
