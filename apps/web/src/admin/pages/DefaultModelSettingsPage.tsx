import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type {
  ModelProvider,
  ModelPurpose,
  ModelType,
  TenantDefaultModel,
} from '../../api/types';
import { findProviderForModel, getModelsForTypes } from '../../lib/providerModels';

// ─── Constants ───────────────────────────────────────────────────────────────

interface PurposeSection {
  purpose: ModelPurpose;
  label: string;
  desc: string;
  glyph: string;
  /** Which ModelType entries in the provider's model_type_map are relevant. */
  modelTypes: ModelType[];
}

const PURPOSE_SECTIONS: PurposeSection[] = [
  {
    purpose: 'llm',
    label: '系统推理模型',
    desc: '用于对话、推理和任务执行的核心语言模型',
    glyph: '⊛',
    modelTypes: ['llm'],
  },
  {
    purpose: 'embedding',
    label: 'Embedding 模型',
    desc: '用于文本向量化，支持检索和 RAG 场景',
    glyph: '⊞',
    modelTypes: ['text_embedding'],
  },
  {
    purpose: 'rerank',
    label: 'Rerank 模型',
    desc: '用于搜索结果重排序，提升检索相关性',
    glyph: '⊟',
    modelTypes: ['rerank'],
  },
  {
    purpose: 'stt',
    label: '语音转文本模型',
    desc: '用于语音识别和语音转文字处理',
    glyph: '⊠',
    modelTypes: [],
  },
  {
    purpose: 'tts',
    label: '文本转语音模型',
    desc: '用于文本到语音合成输出',
    glyph: '⊡',
    modelTypes: [],
  },
];

// ─── Toast ───────────────────────────────────────────────────────────────────

interface Toast {
  id: number;
  message: string;
  kind: 'success' | 'error';
}

let toastCounter = 0;

// ─── Page ────────────────────────────────────────────────────────────────────

export function DefaultModelSettingsPage() {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [defaults, setDefaults] = useState<TenantDefaultModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Per-purpose selection state (model_id). Keyed by purpose.
  const [selections, setSelections] = useState<Record<string, string>>({});
  // Per-purpose saving indicator
  const [savingPurpose, setSavingPurpose] = useState<Record<string, boolean>>({});

  const pushToast = useCallback((message: string, kind: 'success' | 'error') => {
    const id = ++toastCounter;
    setToasts((prev) => [...prev, { id, message, kind }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3000);
  }, []);

  const loadData = useCallback(async () => {
    try {
      setError(null);
      const [providerRes, defaultsRes] = await Promise.all([
        api.admin.llm.listProviders(),
        api.admin.llm.listTenantDefaults(),
      ]);
      setProviders(providerRes.items);
      setDefaults(defaultsRes);

      // Pre-fill selections from current defaults
      const sel: Record<string, string> = {};
      for (const d of defaultsRes) {
        sel[d.purpose] = d.model_id;
      }
      setSelections(sel);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Enabled providers only
  const enabledProviders = useMemo(
    () => providers.filter((p) => p.enabled),
    [providers],
  );

  /** Get the current saved default for a purpose. */
  function getSavedDefault(purpose: ModelPurpose): TenantDefaultModel | undefined {
    return defaults.find((d) => d.purpose === purpose);
  }

  /** Check if the current selection differs from the saved default. */
  function isDirty(purpose: ModelPurpose): boolean {
    const saved = getSavedDefault(purpose);
    const current = selections[purpose];
    if (!saved && !current) return false;
    if (!saved && current) return true;
    if (saved && !current) return true;
    return saved!.model_id !== current;
  }

  async function handleSave(section: PurposeSection) {
    const modelId = selections[section.purpose];
    if (!modelId) return;

    const providerId = findProviderForModel(enabledProviders, modelId, section.modelTypes);
    if (!providerId) {
      pushToast('无法确定该模型所属的 Provider', 'error');
      return;
    }

    setSavingPurpose((prev) => ({ ...prev, [section.purpose]: true }));
    try {
      const result = await api.admin.llm.upsertTenantDefault({
        purpose: section.purpose,
        model_id: modelId,
        provider_id: providerId,
      });
      // Update local defaults
      setDefaults((prev) => {
        const idx = prev.findIndex((d) => d.purpose === section.purpose);
        if (idx >= 0) {
          const updated = [...prev];
          updated[idx] = result;
          return updated;
        }
        return [...prev, result];
      });
      pushToast(`${section.label} 已保存`, 'success');
    } catch (err) {
      pushToast(err instanceof ApiError ? err.message : '保存失败', 'error');
    } finally {
      setSavingPurpose((prev) => ({ ...prev, [section.purpose]: false }));
    }
  }

  function handleSelectChange(purpose: ModelPurpose, modelId: string) {
    setSelections((prev) => ({ ...prev, [purpose]: modelId }));
  }

  return (
    <div className="flex-1 overflow-auto h-full w-full">
      <div className="w-full px-8 py-10 flex flex-col gap-8">
        {/* Header */}
        <div>
          <h1 className="text-[22px] font-display text-ink tracking-[-0.02em]">默认模型设置</h1>
          <p className="text-[12px] text-dim mt-0.5">
            配置租户下所有用户的默认模型。当用户未自行设置偏好时，将使用此处配置的默认模型。
          </p>
        </div>

        {/* Error */}
        {error && (
          <div className="border border-red-200 rounded-md px-4 py-3 bg-red-50 text-[13px] text-red-700">
            {error}
          </div>
        )}

        {/* Loading */}
        {loading && (
          <div className="text-[13px] text-dim py-8 text-center">加载中…</div>
        )}

        {/* Purpose sections */}
        {!loading && (
          <div className="flex flex-col gap-4">
            {PURPOSE_SECTIONS.map((section) => {
              const saved = getSavedDefault(section.purpose);
              const currentModelId = selections[section.purpose] ?? '';
              const dirty = isDirty(section.purpose);
              const saving = savingPurpose[section.purpose] ?? false;

              // Build grouped options for this section
              const groupedOptions: { provider: ModelProvider; models: string[] }[] = [];
              for (const p of enabledProviders) {
                const models = getModelsForTypes(p, section.modelTypes);
                if (models.length > 0) {
                  groupedOptions.push({ provider: p, models });
                }
              }

              // If no grouped options found via model_type_map, show all models from all enabled providers
              if (groupedOptions.length === 0) {
                for (const p of enabledProviders) {
                  if (p.models && p.models.length > 0) {
                    groupedOptions.push({ provider: p, models: p.models });
                  }
                }
              }

              const hasOptions = groupedOptions.length > 0;

              return (
                <div
                  key={section.purpose}
                  className="border border-line rounded-md bg-surface overflow-hidden"
                >
                  <div className="px-5 py-4 flex flex-col gap-3">
                    {/* Section header */}
                    <div className="flex items-center gap-2.5">
                      <span className="w-5 text-center font-mono text-[13px] text-dim">
                        {section.glyph}
                      </span>
                      <div className="flex-1">
                        <div className="text-[14px] font-semibold text-ink">{section.label}</div>
                        <div className="text-[11px] text-dim mt-0.5">{section.desc}</div>
                      </div>
                    </div>

                    {/* Current default info */}
                    <div className="flex items-center gap-3 ml-7">
                      <span className="text-[11px] font-mono text-dim">当前设置：</span>
                      {saved ? (
                        <span className="inline-flex items-center gap-1.5 text-[11px] font-mono text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5">
                          {saved.model_id}
                          <span className="text-[9px] text-dim">
                            ({saved.provider_name ?? saved.provider_id.slice(0, 8)})
                          </span>
                        </span>
                      ) : (
                        <span className="text-[11px] font-mono text-dim">未设置</span>
                      )}
                    </div>

                    {/* Selector + Save */}
                    <div className="flex items-end gap-3 ml-7">
                      <div className="flex-1 max-w-[400px] flex flex-col gap-1">
                        <label className="text-[10px] font-mono text-dim">选择模型</label>
                        <select
                          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                          value={currentModelId}
                          onChange={(e) => handleSelectChange(section.purpose, e.target.value)}
                          disabled={!hasOptions}
                        >
                          <option value="">
                            {hasOptions ? '-- 请选择 --' : '无可用模型'}
                          </option>
                          {groupedOptions.map(({ provider, models }) => (
                            <optgroup key={provider.id} label={provider.name}>
                              {models.map((m) => (
                                <option key={`${provider.id}:${m}`} value={m}>
                                  {m}
                                </option>
                              ))}
                            </optgroup>
                          ))}
                        </select>
                      </div>
                      <button
                        type="button"
                        disabled={!dirty || !currentModelId || saving}
                        onClick={() => handleSave(section)}
                        className="px-3 py-1.5 text-[12px] font-mono bg-ink text-paper rounded-sm hover:opacity-90 disabled:opacity-40 transition-opacity"
                      >
                        {saving ? '保存中…' : '保存'}
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Empty providers hint */}
        {!loading && enabledProviders.length === 0 && (
          <div className="border border-line border-dashed rounded-md px-6 py-8 text-center">
            <div className="text-[13px] text-dim">暂无已启用的 Provider</div>
            <p className="text-[11px] text-dim mt-1">
              请先在「模型管理」页面添加并启用至少一个 Provider，然后再配置默认模型。
            </p>
          </div>
        )}
      </div>

      {/* Toasts */}
      {toasts.length > 0 && (
        <div className="fixed bottom-4 right-4 flex flex-col gap-2 z-50">
          {toasts.map((t) => (
            <div
              key={t.id}
              className={`px-3 py-2 rounded-md text-[12px] font-mono shadow-lg ${
                t.kind === 'success'
                  ? 'bg-green-700 text-white'
                  : 'bg-red-600 text-white'
              }`}
            >
              {t.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
