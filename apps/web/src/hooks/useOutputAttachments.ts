import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { Attachment } from '../api/types';

/**
 * Fetches output attachments for a session and groups them by run_id.
 *
 * - Exposes a `refresh()` function so callers can trigger a refetch (e.g. on
 *   run completion).
 * - Attachments whose `run_id` is null are ignored for inline card rendering
 *   (they have no run to associate with).
 */
export function useOutputAttachments(sessionId: string | undefined): {
  byRunId: Map<string, Attachment[]>;
  refresh: () => void;
} {
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  // Guard against state updates on unmounted component
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const fetch_ = useCallback(async () => {
    if (!sessionId) return;
    try {
      const res = await api.attachments.list(sessionId, 'output');
      if (mountedRef.current) {
        setAttachments(Array.isArray(res?.items) ? res.items : []);
      }
    } catch {
      // failure-open: keep whatever was there
    }
  }, [sessionId]);

  // Initial fetch on mount / sessionId change
  useEffect(() => {
    fetch_();
  }, [fetch_]);

  // Build the runId → Attachment[] map (memoised over attachment list)
  const byRunId = new Map<string, Attachment[]>();
  for (const att of attachments) {
    if (att.run_id == null) continue;
    const existing = byRunId.get(att.run_id);
    if (existing) {
      existing.push(att);
    } else {
      byRunId.set(att.run_id, [att]);
    }
  }

  return { byRunId, refresh: fetch_ };
}
