import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { privacyApi, type PrivacyNotice } from '../../api/privacy';
import { useChatStore } from '../../stores/chatStore';

/**
 * Privacy-notice state: which notice is current, whether this user has
 * acknowledged that version, and the acknowledge action.
 *
 * The server is the source of truth — acknowledgements are recorded with
 * `POST /me/privacy/acknowledgements` and checked with `GET …/latest` on every
 * sign-in, so a new notice `version` is shown again on every device, and so is
 * the notice after the user's data (acknowledgements included) was erased.
 * localStorage is only a per-user fallback for when the server can't be
 * reached.
 *
 * `DataNoticeProvider` (one per signed-in workspace) asks once and shares the
 * answer, so the notice gate and the Privacy tab never disagree: acknowledging
 * in either updates both. Signing out unmounts it; the next sign-in asks again.
 */

/** Version recorded locally when the user acknowledges the built-in fallback summary. */
export const FALLBACK_NOTICE_VERSION = 'static-fallback';

const CACHE_PREFIX = 'privacy-notice-ack:';
// Pre-GDPR-notice flag (static text, localStorage only). Cleared once the
// current notice is acknowledged; it no longer suppresses anything.
const LEGACY_ACK_KEY = 'data-notice-ack-v1';

export interface NoticeAckRecord {
  version: string;
  acknowledged_at: string | null;
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null; // access can throw when storage is disabled
  }
}

function cacheKey(userId: string | null): string {
  return `${CACHE_PREFIX}${userId ?? 'anonymous'}`;
}

export function readCachedAck(userId: string | null): NoticeAckRecord | null {
  try {
    const raw = storage()?.getItem(cacheKey(userId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<NoticeAckRecord>;
    return typeof parsed?.version === 'string' && parsed.version
      ? { version: parsed.version, acknowledged_at: parsed.acknowledged_at ?? null }
      : null;
  } catch {
    return null;
  }
}

export function writeCachedAck(userId: string | null, record: NoticeAckRecord): void {
  try {
    const s = storage();
    s?.setItem(cacheKey(userId), JSON.stringify(record));
    s?.removeItem(LEGACY_ACK_KEY);
  } catch {
    // Storage full / disabled — the server record still holds.
  }
}

function clearCachedAck(userId: string | null): void {
  try {
    storage()?.removeItem(cacheKey(userId));
  } catch {
    // Storage disabled — nothing cached to clear.
  }
}

export interface NoticeResolution {
  /** null when the notice endpoint failed — callers show the built-in summary. */
  notice: PrivacyNotice | null;
  /** What we know this user acknowledged (from the server or the cache). */
  acknowledged: NoticeAckRecord | null;
  /** True when the current notice still has to be shown and acknowledged. */
  needsAck: boolean;
}

export async function resolveNotice(userId: string | null): Promise<NoticeResolution> {
  const cached = readCachedAck(userId);
  let notice: PrivacyNotice | null = null;
  try {
    const n = await privacyApi.notice();
    notice = n && typeof n.version === 'string' && n.version ? n : null;
  } catch {
    notice = null;
  }

  if (!notice) {
    // Can't tell which version is current. Someone who already acknowledged a
    // notice isn't nagged with the fallback; first-time users still see it.
    return { notice: null, acknowledged: cached, needsAck: !cached };
  }

  try {
    const latest = await privacyApi.latestAcknowledgement();
    if (latest?.notice_version === notice.version) {
      const record = { version: notice.version, acknowledged_at: latest.acknowledged_at ?? null };
      writeCachedAck(userId, record);
      return { notice, acknowledged: record, needsAck: false };
    }
    // The server holds no acknowledgement of this version (none at all after an
    // erasure): a cached one must not stand in for it, not even offline later.
    if (cached?.version === notice.version) clearCachedAck(userId);
    const previous = latest?.notice_version
      ? { version: latest.notice_version, acknowledged_at: latest.acknowledged_at ?? null }
      : null;
    return { notice, acknowledged: previous, needsAck: true };
  } catch {
    // Can't verify: a cached acknowledgement of this version stands in until
    // the server answers again. Otherwise show it; acknowledging twice is
    // harmless.
    if (cached?.version === notice.version) return { notice, acknowledged: cached, needsAck: false };
    return { notice, acknowledged: cached, needsAck: true };
  }
}

interface SharedNotice extends NoticeResolution {
  loading: boolean;
  /** Record that the user acknowledged the current notice; rejects when that can't be saved. */
  acknowledge: () => Promise<void>;
}

const DataNoticeContext = createContext<SharedNotice | null>(null);

/** Resolves the signed-in user's notice state once and shares it with every `useDataNotice`. */
export function DataNoticeProvider({ children }: { children: ReactNode }) {
  const userId = useChatStore((s) => s.userProfile?.user_id ?? null);
  // null while the server is being asked.
  const [resolution, setResolution] = useState<NoticeResolution | null>(null);

  useEffect(() => {
    let cancelled = false;
    setResolution(null);
    resolveNotice(userId)
      .catch((): NoticeResolution => ({ notice: null, acknowledged: null, needsAck: true }))
      .then((r) => {
        if (!cancelled) setResolution(r);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const notice = resolution?.notice ?? null;
  const acknowledge = useCallback(async () => {
    // The built-in fallback has no server version to record.
    if (notice) await privacyApi.acknowledgeNotice(notice.version);
    const record = {
      version: notice?.version ?? FALLBACK_NOTICE_VERSION,
      acknowledged_at: new Date().toISOString(),
    };
    writeCachedAck(userId, record);
    // Only if it is still the notice being shown (not one re-asked for since).
    setResolution((r) => (r && r.notice === notice ? { notice, acknowledged: record, needsAck: false } : r));
  }, [notice, userId]);

  const value = useMemo<SharedNotice>(
    () => ({
      loading: resolution === null,
      notice,
      acknowledged: resolution?.acknowledged ?? null,
      needsAck: resolution?.needsAck ?? false,
      acknowledge,
    }),
    [resolution, notice, acknowledge],
  );

  return <DataNoticeContext.Provider value={value}>{children}</DataNoticeContext.Provider>;
}

function useSharedNotice(): SharedNotice {
  const ctx = useContext(DataNoticeContext);
  if (!ctx) throw new Error('useDataNotice must be used within <DataNoticeProvider>');
  return ctx;
}

export interface DataNoticeState {
  loading: boolean;
  notice: PrivacyNotice | null;
  acknowledged: NoticeAckRecord | null;
  needsAck: boolean;
  open: boolean;
  ackBusy: boolean;
  ackError: string | null;
  /** Open the notice (e.g. "Read the privacy notice" on the Privacy tab). */
  show: () => void;
  /** Close without acknowledging; it is shown again next time. */
  dismiss: () => void;
  acknowledge: () => Promise<void>;
}

export const ACK_FAILED_MESSAGE =
  "We couldn't save that just now. Try again, or close this and we'll show it next time.";

/**
 * One view of the shared notice state, with its own dialog open/busy/error.
 *
 * @param autoOpen open the notice by itself when the current version hasn't
 *   been acknowledged (the workspace gate); false for the on-demand Privacy tab view.
 */
export function useDataNotice({ autoOpen }: { autoOpen: boolean }): DataNoticeState {
  const { loading, notice, acknowledged, needsAck, acknowledge: recordAck } = useSharedNotice();
  const [open, setOpen] = useState(false);
  const [ackBusy, setAckBusy] = useState(false);
  const [ackError, setAckError] = useState<string | null>(null);

  // The gate opens once the server has answered, and closes again when the
  // notice gets acknowledged from another view.
  useEffect(() => {
    if (autoOpen && !loading) setOpen(needsAck);
  }, [autoOpen, loading, needsAck]);

  const show = useCallback(() => {
    setAckError(null);
    setOpen(true);
  }, []);

  const dismiss = useCallback(() => {
    setAckError(null);
    setOpen(false);
  }, []);

  const acknowledge = useCallback(async () => {
    setAckBusy(true);
    setAckError(null);
    try {
      await recordAck();
      setOpen(false);
    } catch {
      setAckError(ACK_FAILED_MESSAGE);
    } finally {
      setAckBusy(false);
    }
  }, [recordAck]);

  return { loading, notice, acknowledged, needsAck, open, ackBusy, ackError, show, dismiss, acknowledge };
}
