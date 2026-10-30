import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type { ModelCatalogProvider } from '../api/types';
import { getModelsForTypes } from '../lib/providerModels';
import { MODEL_OPTIONS, type ModelOption } from '../lib/modelOptions';

export interface UseModelOptionsResult {
  /** Dropdown options (real catalogue, MODEL_OPTIONS when unavailable, none
   *  when the tenant has no enabled provider). */
  options: ModelOption[];
  /** Raw provider catalogue returned by GET /llm/models (empty until loaded). */
  providers: ModelCatalogProvider[];
  /** True while the first catalogue fetch is in flight. */
  loading: boolean;
  /** Non-null when the catalogue fetch failed (options still fall back). */
  error: string | null;
  /** The catalogue answered with no enabled provider: nothing can run until an
   *  administrator adds one, so no model is offered. */
  noProvider: boolean;
}

/**
 * Fetch the sanitized provider catalogue (GET /llm/models) and derive the
 * composer's model dropdown options. Mirrors the ProfilePage derivation: each
 * provider contributes the union of its model_type_map['llm'] entries and its
 * flat `models` list, labelled `${id} (${provider.name})`. Falls back to the
 * static MODEL_OPTIONS while the catalogue loads, when the request fails, or
 * when no provider advertises a model, so the composer always has something to
 * render. A catalogue with no enabled provider at all is the exception: the
 * tenant has no model to offer, and a built-in one would only be refused when
 * the run starts. Never throws - on failure it logs, sets `error`, and falls
 * back.
 */
export function useModelOptions(): UseModelOptionsResult {
  const [providers, setProviders] = useState<ModelCatalogProvider[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // Non-admin, sanitized catalogue endpoint (no credentials). Only enabled
    // providers are returned by the BFF, so no client-side filter is needed.
    api.modelPreferences
      .listModels()
      .then((items) => {
        if (cancelled) return;
        setProviders(items);
        setLoaded(true);
        setError(null);
      })
      .catch((err) => {
        if (cancelled) return;
        // Don't fail silently - the dropdown falls back to MODEL_OPTIONS, but a
        // failure here also breaks findProviderForModel, so surface it.
        console.warn('Failed to load model list', err);
        setError('Failed to load the model list; using the built-in list instead.');
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const noProvider = loaded && providers.length === 0;

  // Build option list: enumerate each provider's LLM models (covering both the
  // model_type_map['llm'] entries and the flat `models` list), falling back to
  // the static MODEL_OPTIONS when no provider advertises any model. Memoized so
  // the reference is stable between renders (the composer's auto-correct effect
  // depends on it) and only changes when the catalogue actually loads.
  const options = useMemo<ModelOption[]>(() => {
    if (noProvider) return [];
    const opts = providers.flatMap((p) => {
      const ids = new Set([...getModelsForTypes(p, ['llm']), ...(p.models ?? [])]);
      return Array.from(ids).map((m) => ({ id: m, label: `${m} (${p.name})` }));
    });
    return opts.length > 0 ? opts : MODEL_OPTIONS;
  }, [providers, noProvider]);

  return { options, providers, loading, error, noProvider };
}
