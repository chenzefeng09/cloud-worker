import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { App } from '../api/types';

/**
 * Lists Apps for the given scope (default 'published' — what HomePage shows).
 * Mirrors the loading/error/refetch shape of useSessions so callers can drop
 * it in. Fails open to an empty list rather than poisoning the UI with
 * `undefined`.
 */
export function useApps(scope: 'mine' | 'published' = 'published') {
  const [apps, setApps] = useState<App[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.apps.list(scope);
      setApps(Array.isArray(page?.items) ? page.items : []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { apps, loading, error, refetch };
}
