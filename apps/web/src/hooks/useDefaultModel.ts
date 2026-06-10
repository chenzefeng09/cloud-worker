import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import type { ModelPurpose, ResolvedDefault } from '../api/types';
import { SYSTEM_FALLBACK_MODEL } from '../lib/modelOptions';

// ── Module-level cache ─────────────────────────────────────────────────────
//
// Simple in-memory cache keyed by purpose.  Shared across all hook instances
// so that multiple components rendering `useDefaultModel('llm')` don't each
// fire a separate request.  Cleared by `clearModelCache()` (exported) or by
// the `refetch` returned from the hook.

interface CacheEntry {
  promise: Promise<ResolvedDefault>;
  result: ResolvedDefault | null;
}

const cache: Record<string, CacheEntry> = {};

function resolveWithCache(purpose: ModelPurpose): Promise<ResolvedDefault> {
  const hit = cache[purpose];
  if (hit?.result) return Promise.resolve(hit.result);
  if (hit?.promise) return hit.promise;

  const promise = api.modelPreferences.resolve(purpose).catch(() => {
    // API unavailable — return the hard-coded system fallback so the UI
    // always has *something* to render.
    return {
      purpose,
      model_id: SYSTEM_FALLBACK_MODEL,
      provider_id: '',
      source: 'system' as const,
    };
  });

  cache[purpose] = { promise, result: null };
  promise.then((r) => {
    cache[purpose].result = r;
  });
  return promise;
}

/**
 * Wipe the in-memory cache.  Call after the user updates or deletes a
 * preference so the next `useDefaultModel` invocation fetches fresh data.
 */
export function clearModelCache(purpose?: ModelPurpose): void {
  if (purpose) {
    delete cache[purpose];
  } else {
    for (const key of Object.keys(cache)) {
      delete cache[key];
    }
  }
}

// ── Hook ───────────────────────────────────────────────────────────────────

interface UseDefaultModelResult {
  /** Resolved model id (never null after loading completes). */
  modelId: string | null;
  /** Provider id for the resolved model, or '' for the system fallback. */
  providerId: string | null;
  /** Which layer supplied the value: 'user', 'tenant', or 'system'. */
  source: 'user' | 'tenant' | 'system' | null;
  /** True while the first resolution is in flight. */
  isLoading: boolean;
  /** Non-null only if an unexpected error occurred (fallback is still used). */
  error: Error | null;
  /** Re-fetch from the server, discarding the cached value. */
  refetch: () => void;
}

export function useDefaultModel(purpose: ModelPurpose): UseDefaultModelResult {
  const [modelId, setModelId] = useState<string | null>(null);
  const [providerId, setProviderId] = useState<string | null>(null);
  const [source, setSource] = useState<'user' | 'tenant' | 'system' | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  // Monotonic generation counter guards against stale writes when `purpose`
  // changes while a previous fetch is still in flight.
  const genRef = useRef(0);

  const fetch = useCallback(
    (bypassCache = false) => {
      const gen = ++genRef.current;
      setIsLoading(true);
      setError(null);

      if (bypassCache) {
        delete cache[purpose];
      }

      resolveWithCache(purpose)
        .then((resolved) => {
          if (genRef.current !== gen) return; // stale
          setModelId(resolved.model_id);
          setProviderId(resolved.provider_id);
          setSource(resolved.source);
        })
        .catch((err) => {
          if (genRef.current !== gen) return;
          setError(err instanceof Error ? err : new Error(String(err)));
          // Even on unexpected errors, fall back to the system default.
          setModelId(SYSTEM_FALLBACK_MODEL);
          setProviderId('');
          setSource('system');
        })
        .finally(() => {
          if (genRef.current !== gen) return;
          setIsLoading(false);
        });
    },
    [purpose],
  );

  useEffect(() => {
    fetch();
  }, [fetch]);

  const refetch = useCallback(() => fetch(true), [fetch]);

  return { modelId, providerId, source, isLoading, error, refetch };
}
