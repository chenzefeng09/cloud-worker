import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type {
  ModelProvider,
  ModelPurpose,
  ModelType,
  TenantDefaultModel,
} from '../../api/types';
import { findProviderForModel, getModelsForTypes } from '../../lib/providerModels';
import { GroupedSelect } from './CustomSelect';
import { Dialog } from './Dialog';
import { useToast } from './dialogs';

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

// ─── Dialog ──────────────────────────────────────────────────────────────────

export function DefaultModelsDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [defaults, setDefaults] = useState<TenantDefaultModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Per-purpose selection state (model_id). Keyed by purpose.
  const [selections, setSelections] = useState<Record<string, string>>({});
  // Per-purpose saving indicator
  const [savingPurpose, setSavingPurpose] = useState<Record<string, boolean>>({});

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
      toast.error('无法确定该模型所属的 Provider');
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
      toast.success(`${section.label} 已保存`);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '保存失败');
    } finally {
      setSavingPurpose((prev) => ({ ...prev, [section.purpose]: false }));
    }
  }

  function handleSelectChange(purpose: ModelPurpose, modelId: string) {
    setSelections((prev) => ({ ...prev, [purpose]: modelId }));
  }

  return (
    <Dialog title="默认模型设置" onClose={onClose} className="w-full max-w-xl">
      <p className="text-[11px] text-dim leading-snug">
        配置租户下所有用户的默认模型。当用户未自行设置偏好时，将使用此处配置的默认模型。
      </p>

      {error && (
        <div className="border border-accent-line rounded px-3 py-2 bg-accent-soft text-[12px] text-accent">
          {error}
        </div>
      )}

      {loading && <div className="text-[12px] text-dim py-6 text-center">加载中…</div>}

      {!loading && (
        <div className="flex flex-col gap-2 max-h-[60vh] overflow-auto -mr-1 pr-1">
          {PURPOSE_SECTIONS.map((section) => {
            const saved = getSavedDefault(section.purpose);
            const currentModelId = selections[section.purpose] ?? '';
            const dirty = isDirty(section.purpose);
            const saving = savingPurpose[section.purpose] ?? false;

            // Only surface models whose ModelType matches this section's
            // purpose — never fall back to the provider's flat model list, or an
            // embedding/rerank dropdown would wrongly list LLM models.
            const groupedOptions: { provider: ModelProvider; models: string[] }[] = [];
            // Sections without a backing ModelType (e.g. STT/TTS) have no
            // catalogable models yet — leave their dropdown empty rather than
            // letting getModelsForTypes([]) fall back to the flat LLM list.
            if (section.modelTypes.length > 0) {
              for (const p of enabledProviders) {
                const models = getModelsForTypes(p, section.modelTypes);
                if (models.length > 0) {
                  groupedOptions.push({ provider: p, models });
                }
              }
            }
            const hasOptions = groupedOptions.length > 0;

            return (
              <div
                key={section.purpose}
                className="border border-line rounded bg-paper px-4 py-2.5 flex flex-col gap-2"
              >
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[12px] text-dim leading-none">
                    {section.glyph}
                  </span>
                  <div className="flex-1 min-w-0">
                    <span className="text-[13px] font-semibold text-ink">{section.label}</span>
                    <span className="text-[10px] text-dim ml-2">{section.desc}</span>
                  </div>
                  {saved && (
                    <span className="shrink-0 inline-flex items-center gap-1 text-[10px] font-mono text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5">
                      {saved.model_id}
                      <span className="text-[9px] text-dim">
                        ({saved.provider_name ?? saved.provider_id.slice(0, 8)})
                      </span>
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2 pl-5">
                  <GroupedSelect
                    className="flex-1"
                    groups={groupedOptions.map(({ provider, models }) => ({
                      label: provider.name,
                      options: models.map((m) => ({ value: m, label: m })),
                    }))}
                    value={currentModelId}
                    onChange={(v) => handleSelectChange(section.purpose, v)}
                    placeholder={hasOptions ? '-- 请选择 --' : '无可用模型'}
                    disabled={!hasOptions}
                  />
                  <button
                    type="button"
                    disabled={!dirty || !currentModelId || saving}
                    onClick={() => handleSave(section)}
                    className="shrink-0 px-3 py-1 text-[11px] font-mono bg-ink text-paper rounded-sm hover:opacity-90 disabled:opacity-40 transition-opacity"
                  >
                    {saving ? '…' : '保存'}
                  </button>
                </div>
              </div>
            );
          })}

          {enabledProviders.length === 0 && (
            <div className="border border-line border-dashed rounded px-4 py-6 text-center">
              <div className="text-[12px] text-dim">暂无已启用的 Provider</div>
              <p className="text-[10px] text-dim mt-1">
                请先添加并启用至少一个 Provider，然后再配置默认模型。
              </p>
            </div>
          )}
        </div>
      )}

      <div className="flex justify-end">
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1 text-[12px] rounded-sm border border-line text-dim hover:bg-paper-2 focus:outline-none focus:border-accent"
        >
          关闭
        </button>
      </div>
    </Dialog>
  );
}
