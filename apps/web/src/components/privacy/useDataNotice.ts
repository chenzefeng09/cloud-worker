import { useCallback, useEffect, useState } from 'react';
import { privacyApi, type PrivacyNotice } from '../../api/privacy';
import { useChatStore } from '../../stores/chatStore';

/**
 * Privacy-notice state: which notice is current, whether this user has
 * acknowledged that version, and the acknowledge action.
 *
 * The server is the source of truth — acknowledgements are recorded with
 * `POST /me/privacy/acknowledgements` and checked with `GET …/latest`, so a
 * new notice `version` is shown again on every device. localStorage is only a
 * per-user cache: it skips the `latest` round-trip when it already matches the
 * current version, and covers the case where the server can't be reached.
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
  if (cached?.version === notice.version) {
    return { notice, acknowledged: cached, needsAck: false };
  }

  try {
    const latest = await privacyApi.latestAcknowledgement();
    if (latest?.notice_version === notice.version) {
      const record = { version: notice.version, acknowledged_at: latest.acknowledged_at ?? null };
      writeCachedAck(userId, record);
      return { notice, acknowledged: record, needsAck: false };
    }
    const previous = latest?.notice_version
      ? { version: latest.notice_version, acknowledged_at: latest.acknowledged_at ?? null }
      : cached;
    return { notice, acknowledged: previous, needsAck: true };
  } catch {
    // Can't verify — show it; acknowledging twice is harmless.
    return { notice, acknowledged: cached, needsAck: true };
  }
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
 * @param autoOpen open the notice by itself when the current version hasn't
 *   been acknowledged (the chat gate); false for the on-demand Privacy tab view.
 */
export function useDataNotice({ autoOpen }: { autoOpen: boolean }): DataNoticeState {
  const userId = useChatStore((s) => s.userProfile?.id ?? null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<PrivacyNotice | null>(null);
  const [acknowledged, setAcknowledged] = useState<NoticeAckRecord | null>(null);
  const [needsAck, setNeedsAck] = useState(false);
  const [open, setOpen] = useState(false);
  const [ackBusy, setAckBusy] = useState(false);
  const [ackError, setAckError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    resolveNotice(userId)
      .catch((): NoticeResolution => ({ notice: null, acknowledged: null, needsAck: true }))
      .then((r) => {
        if (cancelled) return;
        setNotice(r.notice);
        setAcknowledged(r.acknowledged);
        setNeedsAck(r.needsAck);
        setOpen(autoOpen && r.needsAck);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, autoOpen]);

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
      // The built-in fallback has no server version to record.
      if (notice) await privacyApi.acknowledgeNotice(notice.version);
      const record = {
        version: notice?.version ?? FALLBACK_NOTICE_VERSION,
        acknowledged_at: new Date().toISOString(),
      };
      writeCachedAck(userId, record);
      setAcknowledged(record);
      setNeedsAck(false);
      setOpen(false);
    } catch {
      setAckError(ACK_FAILED_MESSAGE);
    } finally {
      setAckBusy(false);
    }
  }, [notice, userId]);

  return { loading, notice, acknowledged, needsAck, open, ackBusy, ackError, show, dismiss, acknowledge };
}
