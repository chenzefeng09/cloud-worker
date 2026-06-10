// Shared helpers for resolving which provider owns a given model id, and for
// enumerating a provider's models by ModelType.  Used by both the admin
// "default model settings" page and the per-user profile preference UI so the
// type_map + flat-models lookup logic lives in exactly one place.

import type { ModelCatalogProvider, ModelType, ModelTypeMap } from '../api/types';

// These helpers only read id/name/models/capabilities, so they accept the
// sanitized ModelCatalogProvider subset. A full ModelProvider is structurally
// assignable, so admin call sites keep working unchanged.
type ProviderLike = ModelCatalogProvider;

/** Read the model_type_map out of a provider's capabilities JSONB (may be empty). */
export function getModelTypeMap(provider: ProviderLike): ModelTypeMap {
  const caps = provider.capabilities as Record<string, unknown> | null;
  if (!caps) return {};
  return (caps.model_type_map ?? {}) as ModelTypeMap;
}

/**
 * Gather all model ids from a provider that match the given model types.
 * When `types` is empty, falls back to the provider's flat `models` list.
 */
export function getModelsForTypes(provider: ProviderLike, types: ModelType[]): string[] {
  const typeMap = getModelTypeMap(provider);
  const ids = new Set<string>();
  for (const t of types) {
    for (const m of typeMap[t] ?? []) {
      ids.add(m);
    }
  }
  // Fallback: if no type map entries found but provider has a flat models list
  if (ids.size === 0 && types.length === 0 && provider.models) {
    for (const m of provider.models) ids.add(m);
  }
  return Array.from(ids);
}

/**
 * Find the provider id that owns `modelId`, checking the model_type_map first
 * (scoped to `modelTypes`) and then falling back to each provider's flat
 * `models` list.  Returns null when no enabled provider advertises the model.
 */
export function findProviderForModel(
  providers: ProviderLike[],
  modelId: string,
  modelTypes: ModelType[],
): string | null {
  for (const p of providers) {
    if (getModelsForTypes(p, modelTypes).includes(modelId)) return p.id;
  }
  // Fallback: search the flat models list (covers providers without a type map).
  for (const p of providers) {
    if (p.models?.includes(modelId)) return p.id;
  }
  return null;
}
