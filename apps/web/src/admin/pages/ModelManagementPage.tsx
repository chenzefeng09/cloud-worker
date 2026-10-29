import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
import { useConfirm } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import { DefaultModelsDialog } from '../components/DefaultModelsDialog';
import type {
  ModelProvider,
  ModelType,
  ModelTypeMap,
  ProbeCapabilityResult,
  ProbeReport,
  ProviderType,
  RegisterProviderRequest,
  ModelSetting,
  ModelSettingsMap,
} from '../../api/types';

// ─── Constants ───────────────────────────────────────────────────────────────

const PROVIDER_OPTIONS: { value: ProviderType; label: string }[] = [
  { value: 'openai_compatible', label: 'OpenAI compatible' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'vllm', label: 'vLLM' },
  { value: 'ollama', label: 'Ollama' },
  { value: 'bedrock', label: 'AWS Bedrock' },
  { value: 'azure', label: 'Azure OpenAI' },
  { value: 'zhipu', label: 'Zhipu AI' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'moonshot', label: 'Moonshot' },
  { value: 'minimax', label: 'MiniMax' },
  { value: 'aliyun', label: 'Alibaba Cloud Bailian' },
  { value: 'baidu', label: 'Baidu AI Cloud (Qianfan)' },
  { value: 'tencent', label: 'Tencent Cloud (Hunyuan)' },
];

const MODEL_TYPES: { value: ModelType; label: string; desc: string }[] = [
  { value: 'llm', label: 'LLM', desc: 'Language model (chat / reasoning)' },
  { value: 'text_embedding', label: 'Embedding model', desc: 'Text embeddings (retrieval / RAG)' },
  { value: 'rerank', label: 'Rerank model', desc: 'Search result reranking' },
];

function providerKindLabel(kind: string): string {
  return PROVIDER_OPTIONS.find((o) => o.value === kind)?.label ?? kind;
}

/** Get the model_type_map from a provider's capabilities JSONB. */

/** Get the model_settings from a provider's capabilities JSONB. */
function getModelSettingsMap(provider: ModelProvider): ModelSettingsMap {
  const caps = provider.capabilities as Record<string, unknown> | null;
  if (!caps) return {};
  return (caps.model_settings ?? {}) as ModelSettingsMap;
}

function getModelTypeMap(provider: ModelProvider): ModelTypeMap {
  const caps = provider.capabilities as Record<string, unknown> | null;
  if (!caps) return {};
  return (caps.model_type_map ?? {}) as ModelTypeMap;
}

/** Total model count across all types. */
function totalModelCount(provider: ModelProvider): number {
  const typeMap = getModelTypeMap(provider);
  let n = 0;
  for (const models of Object.values(typeMap)) {
    n += (models as string[])?.length ?? 0;
  }
  // Fallback to provider.models if type map is empty
  if (n === 0 && provider.models) return provider.models.length;
  return n;
}

// ─── Small Components ────────────────────────────────────────────────────────

function ModelTypeBadge({ type }: { type: ModelType }) {
  const spec = MODEL_TYPES.find((t) => t.value === type);
  const colors: Record<ModelType, string> = {
    llm: 'text-blue-700 bg-blue-50 border-blue-200',
    text_embedding: 'text-violet-700 bg-violet-50 border-violet-200',
    rerank: 'text-amber-700 bg-amber-50 border-amber-200',
  };
  return (
    <span
      className={`inline-flex items-center font-mono text-[10px] tracking-[0.04em] border rounded-sm px-1.5 py-0.5 ${colors[type] ?? 'text-dim bg-paper-2 border-line'}`}
    >
      {spec?.label ?? type}
    </span>
  );
}

function EnabledDot({ enabled }: { enabled: boolean }) {
  return (
    <span
      className={`inline-block w-1.5 h-1.5 rounded-full ${enabled ? 'bg-green-500' : 'bg-gray-300'}`}
      title={enabled ? 'Enabled' : 'Disabled'}
    />
  );
}

function isProbePass(val: ProbeCapabilityResult): boolean {
  return val === true;
}

function isProbeUnknown(val: ProbeCapabilityResult): boolean {
  return val === 'unknown' || (typeof val === 'string' && !val.startsWith('FAILED') && val !== 'true' && val !== 'false');
}

// ─── Add Provider Dialog ─────────────────────────────────────────────────────

interface AddProviderDialogProps {
  open: boolean;
  onClose: () => void;
  onAdded: () => void;
}

interface SelectedModel {
  id: string;
  model_type: ModelType;
  context_length: string;
  max_tokens: string;
  vision_support: boolean;
  structured_output: boolean;
}

function AddProviderDialog({ open, onClose, onAdded }: AddProviderDialogProps) {
  const [name, setName] = useState('');
  const [apiBase, setApiBase] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [providerType, setProviderType] = useState<ProviderType>('openai_compatible');

  const [probing, setProbing] = useState(false);
  const [probeReport, setProbeReport] = useState<ProbeReport | null>(null);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [fetchedModels, setFetchedModels] = useState<string[]>([]);

  // Selected models detailed config
  const [selectedModels, setSelectedModels] = useState<SelectedModel[]>([]);
  const [newModelId, setNewModelId] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const resetForm = useCallback(() => {
    setName('');
    setApiBase('');
    setApiKey('');
    setProviderType('openai_compatible');
    setProbing(false);
    setProbeReport(null);
    setProbeError(null);
    setFetchedModels([]);
    setSelectedModels([]);
    setNewModelId('');
    setSubmitting(false);
    setSubmitError(null);
  }, []);

  useEffect(() => {
    if (open) resetForm();
  }, [open, resetForm]);

  const handleProbe = async () => {
    if (!apiBase.trim() || !apiKey.trim()) return;
    setProbing(true);
    setProbeError(null);
    setProbeReport(null);
    try {
      const report = await api.admin.llm.probe({
        url: apiBase.trim(),
        api_key: apiKey,
        provider_type: providerType,
      });
      setProbeReport(report);
      const modelsFromProbe = report.results.model_list;
      if (modelsFromProbe && modelsFromProbe.length > 0) {
        setFetchedModels(modelsFromProbe);
        if (selectedModels.length === 0) {
          const modelDetails = report.results.model_details;
          setSelectedModels(modelsFromProbe.slice(0, 5).map(id => {
            const details = modelDetails?.[id];
            return {
              id,
              model_type: 'llm',
              context_length: details?.context_length != null ? String(details.context_length) : '',
              max_tokens: details?.max_tokens != null ? String(details.max_tokens) : '',
              vision_support: false,
              structured_output: false,
            };
          }));
        }
      }
    } catch (err) {
      setProbeError(err instanceof ApiError ? err.message : 'Verification failed');
    } finally {
      setProbing(false);
    }
  };

  const handleAddModel = (id: string) => {
    if (!id.trim() || selectedModels.some(m => m.id === id.trim())) return;
    setSelectedModels([...selectedModels, {
      id: id.trim(),
      model_type: 'llm',
      context_length: '',
      max_tokens: '',
      vision_support: false,
      structured_output: false,
    }]);
    setNewModelId('');
  };

  const updateSelectedModel = (idx: number, field: keyof SelectedModel, value: any) => {
    const updated = [...selectedModels];
    updated[idx] = { ...updated[idx], [field]: value };
    setSelectedModels(updated);
  };

  const removeSelectedModel = (idx: number) => {
    setSelectedModels(selectedModels.filter((_, i) => i !== idx));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !apiBase.trim() || !apiKey.trim()) return;

    if (selectedModels.length === 0) {
      setSubmitError('Configure at least one model');
      return;
    }

    const allModels = selectedModels.map(m => m.id);
    const modelTypeMap: ModelTypeMap = {};
    const modelSettings: ModelSettingsMap = {};

    for (const sm of selectedModels) {
      if (!modelTypeMap[sm.model_type]) {
        modelTypeMap[sm.model_type] = [];
      }
      modelTypeMap[sm.model_type]!.push(sm.id);

      const ms: ModelSetting = { model_type: sm.model_type };
      if (sm.context_length) ms.context_length = parseInt(sm.context_length, 10);
      if (sm.max_tokens) ms.max_tokens = parseInt(sm.max_tokens, 10);
      if (sm.vision_support) ms.vision_support = true;
      if (sm.structured_output) ms.structured_output = true;

      modelSettings[sm.id] = ms;
    }

    const body: RegisterProviderRequest = {
      name: name.trim(),
      api_base: apiBase.trim(),
      api_key: apiKey,
      provider_type: providerType,
      models: allModels,
      model_type_map: modelTypeMap,
      model_settings: modelSettings,
    };

    setSubmitting(true);
    setSubmitError(null);
    try {
      await api.admin.llm.registerProvider(body);
      onAdded();
      onClose();
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : 'Registration failed');
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const canProbe = apiBase.trim() && apiKey.trim();
  const probeOk = probeReport && isProbePass(probeReport.results.list_models);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[5vh] pb-[5vh] bg-black/30" onClick={onClose}>
      <div
        className="bg-paper border border-line rounded-lg shadow-xl w-[720px] h-full max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 pt-5 pb-3 border-b border-line shrink-0">
          <h2 className="text-[15px] font-display text-ink tracking-[-0.02em]">Add provider</h2>
          <p className="text-[11px] text-dim mt-0.5">Configure the provider's connection details and available models</p>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5">
          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] font-mono text-dim">Name</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                placeholder="e.g. DeepSeek Production"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] font-mono text-dim">Provider type</label>
              <CustomSelect
                options={PROVIDER_OPTIONS}
                value={providerType}
                onChange={(v) => setProviderType(v as ProviderType)}
              />
            </div>
            <div className="flex flex-col gap-1 col-span-2 md:col-span-1">
              <label className="text-[11px] font-mono text-dim">Base URL</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                placeholder="e.g. https://api.deepseek.com/v1"
                value={apiBase}
                onChange={(e) => setApiBase(e.target.value)}
                required
              />
            </div>
            <div className="flex flex-col gap-1 col-span-2 md:col-span-1">
              <label className="text-[11px] font-mono text-dim">API Key</label>
              <input
                type="password"
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                placeholder="sk-..."
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                required
                autoComplete="new-password"
              />
            </div>
          </div>

          <div className="border border-line rounded-md p-3 bg-paper-2">
            <div className="flex items-center gap-3">
              <button
                type="button"
                disabled={!canProbe || probing}
                onClick={handleProbe}
                className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-surface disabled:opacity-50"
              >
                {probing ? 'Verifying…' : 'Verify connection and refresh models'}
              </button>
              {probeOk && (
                <span className="text-[11px] text-green-700 font-mono">
                  ✓ Connected · found {fetchedModels.length || probeReport.results.model_count || '?'} models
                </span>
              )}
              {probeReport && !probeOk && (
                <span className="text-[11px] text-amber-700 font-mono">
                  ⚠ Some capabilities are not supported
                </span>
              )}
            </div>
            {probeError && (
              <div className="mt-2 text-[11px] text-red-600">{probeError}</div>
            )}
            {probeReport && (
              <div className="mt-3 grid grid-cols-2 gap-1 text-[10.5px] font-mono">
                {[
                  ['Model list', probeReport.results.list_models],
                  ['Function Calling', probeReport.results.function_calling],
                  ['Streaming', probeReport.results.streaming],
                  ['Context ≥32K', probeReport.results.context_window_32k],
                  ['Parallel tools', probeReport.results.parallel_tool_calls],
                ].map(([label, val]) => (
                  <div key={label as string} className="flex items-center gap-1.5">
                    <span className={isProbePass(val as ProbeCapabilityResult) ? 'text-green-600' : isProbeUnknown(val as ProbeCapabilityResult) ? 'text-amber-600' : 'text-red-500'}>
                      {isProbePass(val as ProbeCapabilityResult) ? '✓' : isProbeUnknown(val as ProbeCapabilityResult) ? '?' : '✗'}
                    </span>
                    <span className="text-dim">{label as string}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink font-semibold">Models ({selectedModels.length})</div>
            </div>

            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <input
                  list="fetched-models-list"
                  className="w-full border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono"
                  placeholder="Enter a model ID or pick from the list…"
                  value={newModelId}
                  onChange={(e) => setNewModelId(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddModel(newModelId);
                    }
                  }}
                />
                <datalist id="fetched-models-list">
                  {fetchedModels.map(m => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </div>
              <button
                type="button"
                onClick={() => handleAddModel(newModelId)}
                disabled={!newModelId.trim()}
                className="px-3 py-1.5 text-[12px] bg-paper border border-line rounded-sm hover:bg-paper-2 disabled:opacity-50"
              >
                Add
              </button>
            </div>

            {selectedModels.length > 0 && (
              <div className="flex flex-col gap-2 mt-1">
                {selectedModels.map((sm, idx) => (
                  <div key={idx} className="border border-line rounded-sm bg-surface p-3 flex flex-col gap-3">
                    <div className="flex items-center justify-between">
                      <span className="text-[13px] font-mono text-ink font-semibold">{sm.id}</span>
                      <button type="button" onClick={() => removeSelectedModel(idx)} className="text-[11px] text-red-500 hover:text-red-700">Remove</button>
                    </div>
                    
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-start">
                      <div className="flex flex-col gap-1">
                        <label className="text-[10px] text-dim">Type</label>
                        <CustomSelect
                          options={MODEL_TYPES}
                          value={sm.model_type}
                          onChange={(v) => updateSelectedModel(idx, 'model_type', v)}
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <label className="text-[10px] text-dim">Context length</label>
                        <input
                          type="number"
                          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper font-mono"
                          placeholder="e.g. 128000"
                          value={sm.context_length}
                          onChange={(e) => updateSelectedModel(idx, 'context_length', e.target.value)}
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <label className="text-[10px] text-dim">Max Tokens</label>
                        <input
                          type="number"
                          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper font-mono"
                          placeholder="e.g. 4096"
                          value={sm.max_tokens}
                          onChange={(e) => updateSelectedModel(idx, 'max_tokens', e.target.value)}
                        />
                      </div>
                    </div>
                    <div className="flex items-center gap-5">
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={sm.vision_support}
                          onChange={(e) => updateSelectedModel(idx, 'vision_support', e.target.checked)}
                        />
                        <span className="text-[11px] text-ink-2">Vision</span>
                      </label>
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={sm.structured_output}
                          onChange={(e) => updateSelectedModel(idx, 'structured_output', e.target.checked)}
                        />
                        <span className="text-[11px] text-ink-2">Structured output</span>
                      </label>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="px-5 py-3 border-t border-line shrink-0 flex items-center justify-between">
          <div className="text-[12px] text-red-600">{submitError}</div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm hover:bg-paper-2"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={submitting || !name.trim() || !apiBase.trim() || !apiKey.trim()}
              className="px-4 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
            >
              {submitting ? 'Saving…' : 'Save provider'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}



// ─── Edit Provider Dialog ────────────────────────────────────────────────────

interface EditProviderDialogProps {
  open: boolean;
  provider: ModelProvider | null;
  onClose: () => void;
  onUpdated: () => void;
}

function EditProviderDialog({ open, provider, onClose, onUpdated }: EditProviderDialogProps) {
  const [name, setName] = useState('');
  
  // Selected models detailed config
  const [selectedModels, setSelectedModels] = useState<SelectedModel[]>([]);
  const [newModelId, setNewModelId] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (open && provider) {
      setName(provider.name);
      
      const typeMap = getModelTypeMap(provider);
      const settingsMap = getModelSettingsMap(provider);
      const initialModels: SelectedModel[] = [];
      
      const activeTypes = (Object.keys(typeMap) as ModelType[]).filter(k => (typeMap[k]?.length ?? 0) > 0);
      if (activeTypes.length > 0) {
        for (const t of activeTypes) {
          for (const m of typeMap[t]!) {
            const s = settingsMap[m];
            initialModels.push({
              id: m,
              model_type: t,
              context_length: s?.context_length ? String(s.context_length) : '',
              max_tokens: s?.max_tokens ? String(s.max_tokens) : '',
              vision_support: s?.vision_support || false,
              structured_output: s?.structured_output || false,
            });
          }
        }
      } else if (provider.models) {
        // Fallback for legacy data
        for (const m of provider.models) {
          initialModels.push({
            id: m,
            model_type: 'llm',
            context_length: '',
            max_tokens: '',
            vision_support: false,
            structured_output: false,
          });
        }
      }
      setSelectedModels(initialModels);
      setNewModelId('');
      setSubmitting(false);
      setSubmitError(null);
    }
  }, [open, provider]);

  const handleAddModel = (id: string) => {
    if (!id.trim() || selectedModels.some(m => m.id === id.trim())) return;
    setSelectedModels([...selectedModels, {
      id: id.trim(),
      model_type: 'llm',
      context_length: '',
      max_tokens: '',
      vision_support: false,
      structured_output: false,
    }]);
    setNewModelId('');
  };

  const updateSelectedModel = (idx: number, field: keyof SelectedModel, value: any) => {
    const updated = [...selectedModels];
    updated[idx] = { ...updated[idx], [field]: value };
    setSelectedModels(updated);
  };

  const removeSelectedModel = (idx: number) => {
    setSelectedModels(selectedModels.filter((_, i) => i !== idx));
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!provider || !name.trim()) return;

    if (selectedModels.length === 0) {
      setSubmitError('Configure at least one model');
      return;
    }

    const allModels = selectedModels.map(m => m.id);
    const modelTypeMap: ModelTypeMap = {};
    const modelSettings: ModelSettingsMap = {};

    for (const sm of selectedModels) {
      if (!modelTypeMap[sm.model_type]) {
        modelTypeMap[sm.model_type] = [];
      }
      modelTypeMap[sm.model_type]!.push(sm.id);

      const ms: ModelSetting = { model_type: sm.model_type };
      if (sm.context_length) ms.context_length = parseInt(sm.context_length, 10);
      if (sm.max_tokens) ms.max_tokens = parseInt(sm.max_tokens, 10);
      if (sm.vision_support) ms.vision_support = true;
      if (sm.structured_output) ms.structured_output = true;

      modelSettings[sm.id] = ms;
    }
    
    const body = {
      name: name.trim(),
      models: allModels,
      model_type_map: modelTypeMap,
      model_settings: modelSettings,
    };

    setSubmitting(true);
    setSubmitError(null);
    try {
      await api.admin.llm.updateProvider(provider.id, body);
      onUpdated();
      onClose();
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : 'Update failed');
    } finally {
      setSubmitting(false);
    }
  };

  if (!open || !provider) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[5vh] pb-[5vh] bg-black/30" onClick={onClose}>
      <div
        className="bg-paper border border-line rounded-lg shadow-xl w-[720px] h-full max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 pt-5 pb-3 border-b border-line shrink-0">
          <h2 className="text-[15px] font-display text-ink tracking-[-0.02em]">Edit provider</h2>
          <p className="text-[11px] text-dim mt-0.5">Edit the {providerKindLabel(provider.kind)} provider configuration and model list</p>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-5">
          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] font-mono text-dim">Name</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="flex flex-col gap-1 col-span-2 md:col-span-1">
              <label className="text-[11px] font-mono text-dim">Base URL (read-only)</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper-2 text-dim font-mono cursor-not-allowed"
                value={provider.base_url}
                disabled
              />
            </div>
          </div>

          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-ink font-semibold">Models ({selectedModels.length})</div>
            </div>

            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <input
                  className="w-full border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono"
                  placeholder="Enter a new model ID…"
                  value={newModelId}
                  onChange={(e) => setNewModelId(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddModel(newModelId);
                    }
                  }}
                />
              </div>
              <button
                type="button"
                onClick={() => handleAddModel(newModelId)}
                disabled={!newModelId.trim()}
                className="px-3 py-1.5 text-[12px] bg-paper border border-line rounded-sm hover:bg-paper-2 disabled:opacity-50"
              >
                Add
              </button>
            </div>

            {selectedModels.length > 0 && (
              <div className="flex flex-col gap-2 mt-1">
                {selectedModels.map((sm, idx) => (
                  <div key={idx} className="border border-line rounded-sm bg-surface p-3 flex flex-col gap-3">
                    <div className="flex items-center justify-between">
                      <span className="text-[13px] font-mono text-ink font-semibold">{sm.id}</span>
                      <button type="button" onClick={() => removeSelectedModel(idx)} className="text-[11px] text-red-500 hover:text-red-700">Remove</button>
                    </div>
                    
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-start">
                      <div className="flex flex-col gap-1">
                        <label className="text-[10px] text-dim">Type</label>
                        <CustomSelect
                          options={MODEL_TYPES}
                          value={sm.model_type}
                          onChange={(v) => updateSelectedModel(idx, 'model_type', v)}
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <label className="text-[10px] text-dim">Context length</label>
                        <input
                          type="number"
                          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper font-mono"
                          placeholder="e.g. 128000"
                          value={sm.context_length}
                          onChange={(e) => updateSelectedModel(idx, 'context_length', e.target.value)}
                        />
                      </div>
                      <div className="flex flex-col gap-1">
                        <label className="text-[10px] text-dim">Max Tokens</label>
                        <input
                          type="number"
                          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper font-mono"
                          placeholder="e.g. 4096"
                          value={sm.max_tokens}
                          onChange={(e) => updateSelectedModel(idx, 'max_tokens', e.target.value)}
                        />
                      </div>
                    </div>
                    <div className="flex items-center gap-5">
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={sm.vision_support}
                          onChange={(e) => updateSelectedModel(idx, 'vision_support', e.target.checked)}
                        />
                        <span className="text-[11px] text-ink-2">Vision</span>
                      </label>
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={sm.structured_output}
                          onChange={(e) => updateSelectedModel(idx, 'structured_output', e.target.checked)}
                        />
                        <span className="text-[11px] text-ink-2">Structured output</span>
                      </label>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="px-5 py-3 border-t border-line shrink-0 flex items-center justify-between">
          <div className="text-[12px] text-red-600">{submitError}</div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm hover:bg-paper-2"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={submitting || !name.trim()}
              className="px-4 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
            >
              {submitting ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Provider Card ───────────────────────────────────────────────────────────

interface ProviderCardProps {
  provider: ModelProvider;
  expanded: boolean;
  onToggleExpand: () => void;
  onToggleEnabled: () => void;
  onDelete: () => void;
  onEdit: () => void;
}

function ProviderCard({ provider, expanded, onToggleExpand, onToggleEnabled, onDelete, onEdit }: ProviderCardProps) {
  const typeMap = getModelTypeMap(provider);
  const settingsMap = getModelSettingsMap(provider);
  const modelCount = totalModelCount(provider);
  const activeTypes = (Object.keys(typeMap) as ModelType[]).filter((k) => (typeMap[k]?.length ?? 0) > 0);

  return (
    <div
      className={[
        'border rounded-md overflow-hidden transition-colors',
        provider.enabled ? 'border-line bg-surface' : 'border-line-2 bg-paper-2 opacity-70',
      ].join(' ')}
    >
      {/* Card header */}
      <div
        className="px-6 py-4 flex items-center gap-4 cursor-pointer hover:bg-paper-2/50 select-none"
        onClick={onToggleExpand}
      >
        <EnabledDot enabled={provider.enabled} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[15px] font-semibold text-ink truncate">{provider.name}</span>
            <span className="text-[10.5px] font-mono text-dim border border-line rounded-sm px-1 py-0.5">
              {providerKindLabel(provider.kind)}
            </span>
          </div>
          <div className="flex items-center gap-2 mt-1">
            {activeTypes.map((t) => (
              <ModelTypeBadge key={t} type={t} />
            ))}
            <span className="text-[10.5px] text-dim font-mono">{modelCount} models</span>
          </div>
        </div>
        <span className="text-[10px] text-dim font-mono">{expanded ? '▲' : '▼'}</span>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-line px-6 py-5 flex flex-col gap-5 bg-paper">
          {/* Connection info */}
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[11px]">
            <span className="font-mono text-dim">Base URL</span>
            <span className="font-mono text-ink-2 truncate">{provider.base_url}</span>
            <span className="font-mono text-dim">Default model</span>
            <span className="font-mono text-ink-2">{provider.default_model || '—'}</span>
          </div>

          {/* Models by type */}
          {activeTypes.length > 0 && (
            <div className="flex flex-col gap-2">
              {activeTypes.map((t) => {
                const models = typeMap[t] ?? [];
                return (
                  <div key={t}>
                    <div className="flex items-center gap-1.5 mb-1">
                      <ModelTypeBadge type={t} />
                      <span className="text-[10.5px] text-dim font-mono">{models.length}</span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {models.map((m) => {
                        const s = settingsMap[m];
                        return (
                          <span
                            key={m}
                            className="inline-flex items-center gap-1 text-[11px] font-mono text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5"
                          >
                            {m}
                            {s?.vision_support && <span className="text-[9px] bg-blue-100 text-blue-700 px-0.5 rounded border border-blue-200 leading-none py-[2px]" title="Supports vision">👁</span>}
                            {s?.structured_output && <span className="text-[9px] bg-purple-100 text-purple-700 px-0.5 rounded border border-purple-200 leading-none py-[2px]" title="Structured output">JSON</span>}
                          </span>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Fallback: show raw models array if no type map */}
          {activeTypes.length === 0 && provider.models && provider.models.length > 0 && (
            <div>
              <div className="text-[10.5px] font-mono text-dim mb-1">Models</div>
              <div className="flex flex-wrap gap-1">
                {provider.models.map((m) => {
                  const s = settingsMap[m];
                  return (
                    <span
                      key={m}
                      className="inline-flex items-center gap-1 text-[11px] font-mono text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5"
                    >
                      {m}
                      {s?.vision_support && <span className="text-[9px] bg-blue-100 text-blue-700 px-0.5 rounded border border-blue-200 leading-none py-[2px]" title="Supports vision">👁</span>}
                      {s?.structured_output && <span className="text-[9px] bg-purple-100 text-purple-700 px-0.5 rounded border border-purple-200 leading-none py-[2px]" title="Structured output">JSON</span>}
                    </span>
                  );
                })}
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="flex items-center gap-2 pt-1 border-t border-line-2">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onEdit();
              }}
              className="px-2.5 py-1 text-[11px] font-mono text-ink border border-line rounded-sm hover:bg-paper-2"
            >
              Edit
            </button>

            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onToggleEnabled();
              }}
              className={[
                'px-2.5 py-1 text-[11px] font-mono rounded-sm border',
                provider.enabled
                  ? 'text-amber-700 border-amber-200 hover:bg-amber-50'
                  : 'text-green-700 border-green-200 hover:bg-green-50',
              ].join(' ')}
            >
              {provider.enabled ? 'Disable' : 'Enable'}
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              className="px-2.5 py-1 text-[11px] font-mono text-red-600 border border-red-200 rounded-sm hover:bg-red-50"
            >
              Delete
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function ModelManagementPage() {
  // Scope split: the platform super_admin manages the shared LLM provider
  // directory (接入点目录) AND the per-tenant default-model preferences; a
  // tenant_admin (model.read / model.admin) only chooses their own tenant's
  // default models and never sees the provider-management surface.  This mirrors
  // the org/quota delegation pattern — visibility tracks the BFF guard tier
  // (provider writes → super_admin; tenant-defaults → tenant_admin).
  const userProfile = useChatStore((s) => s.userProfile);
  const canManageProviders = isPlatformAdmin(userProfile?.permissions);

  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [editingProvider, setEditingProvider] = useState<ModelProvider | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);
  // For tenant admins the whole page IS the default-model picker, so open it by
  // default; platform admins toggle it from the provider-management header.
  const [showDefaults, setShowDefaults] = useState(!canManageProviders);
  const confirm = useConfirm();

  const loadProviders = useCallback(async () => {
    try {
      setError(null);
      const data = await api.admin.llm.listProviders();
      setProviders(data.items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Provider list management is platform-admin only.  A tenant_admin neither
    // renders nor needs the provider cards here (the default-model dialog loads
    // its own provider catalogue via the model.read lane), so skip the fetch.
    if (!canManageProviders) {
      setLoading(false);
      return;
    }
    loadProviders();
  }, [loadProviders, canManageProviders]);

  const handleToggleEnabled = async (provider: ModelProvider) => {
    // Disabling a Provider takes its models offline platform-wide, so confirm
    // first. Re-enabling is harmless and needs no confirmation.
    if (
      provider.enabled &&
      !(await confirm({
        title: 'Disable provider',
        message: `Disable provider "${provider.name}"? All of its models will become unavailable.`,
        tone: 'danger',
        confirmText: 'Disable',
      }))
    )
      return;
    setToggling(provider.id);
    try {
      await api.admin.llm.updateProvider(provider.id, { enabled: !provider.enabled });
      await loadProviders();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Operation failed');
    } finally {
      setToggling(null);
    }
  };

  const handleDelete = async (provider: ModelProvider) => {
    if (
      !(await confirm({
        title: 'Delete provider',
        message: `Delete provider "${provider.name}"? This cannot be undone.`,
        tone: 'danger',
        confirmText: 'Delete',
      }))
    )
      return;
    setDeleting(provider.id);
    try {
      await api.admin.llm.deleteProvider(provider.id);
      if (expandedId === provider.id) setExpandedId(null);
      await loadProviders();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Delete failed');
    } finally {
      setDeleting(null);
    }
  };

  // Summary counts by model type
  const typeCounts: Record<ModelType, number> = { llm: 0, text_embedding: 0, rerank: 0 };
  for (const p of providers) {
    const typeMap = getModelTypeMap(p);
    let hasTypeMap = false;
    for (const t of MODEL_TYPES) {
      const count = typeMap[t.value]?.length ?? 0;
      typeCounts[t.value] += count;
      if (count > 0) hasTypeMap = true;
    }
    // If no type map exists, assume all models are 'llm' by default
    if (!hasTypeMap && p.models) {
      typeCounts.llm += p.models.length;
    }
  }

  return (
    <div className="flex-1 overflow-auto h-full w-full">
      <div className="w-full px-8 py-10 flex flex-col gap-8">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-[22px] font-display text-ink tracking-[-0.02em]">Model management</h1>
          <p className="text-[12px] text-dim mt-0.5">
            {canManageProviders
              ? 'Manage provider connections for LLM, embedding and rerank models'
              : 'Configure this tenant’s default model preferences'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowDefaults(true)}
            className="px-4 py-2 border border-line bg-paper text-ink text-[13px] font-mono rounded-md shadow-sm hover:bg-paper-2 transition-colors"
          >
            Default model settings
          </button>
          {/* Adding a Provider mutates the shared platform directory → super_admin only. */}
          {canManageProviders && providers.length > 0 && (
            <button
              type="button"
              onClick={() => setShowAdd(true)}
              className="px-4 py-2 bg-ink text-paper text-[13px] font-mono rounded-md shadow hover:bg-ink-2 transition-colors"
            >
              + Add provider
            </button>
          )}
        </div>
      </div>

      {/* Provider directory management — platform super_admin only.  A
          tenant_admin sees just the default-model picker below. */}
      {canManageProviders ? (
        <>
          {/* Model type summary */}
          <div className="flex gap-3">
            {MODEL_TYPES.map((mt) => (
              <div
                key={mt.value}
                className="flex-1 border border-line rounded-lg px-5 py-4 bg-paper shadow-sm"
              >
                <div className="flex items-center gap-1.5">
                  <ModelTypeBadge type={mt.value} />
                </div>
                <div className="mt-1.5 text-[20px] font-display text-ink tracking-[-0.02em]">
                  {typeCounts[mt.value]}
                </div>
                <div className="text-[10.5px] text-dim font-mono">{mt.desc}</div>
              </div>
            ))}
          </div>

          {/* Error */}
          {error && (
            <div className="border border-red-200 rounded-md px-4 py-3 bg-red-50 text-[13px] text-red-700">
              {error}
            </div>
          )}

          {/* Loading */}
          {loading && (
            <div className="text-[13px] text-dim py-8 text-center">Loading…</div>
          )}

          {/* Empty state */}
          {!loading && providers.length === 0 && (
            <div className="border border-line border-dashed rounded-md px-6 py-10 text-center">
              <div className="text-[14px] text-dim">No model providers configured yet</div>
              <p className="text-[12px] text-dim mt-1">
                Add a provider to connect LLM, embedding and rerank models
              </p>
              <button
                type="button"
                onClick={() => setShowAdd(true)}
                className="mt-3 px-3 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90"
              >
                + Add provider
              </button>
            </div>
          )}

          {/* Provider list */}
          {!loading && providers.length > 0 && (
            <div className="flex flex-col gap-2">
              {providers.map((p) => (
                <ProviderCard
                  key={p.id}
                  provider={p}
                  expanded={expandedId === p.id}
                  onToggleExpand={() => setExpandedId(expandedId === p.id ? null : p.id)}
                  onToggleEnabled={() => handleToggleEnabled(p)}
                  onDelete={() => handleDelete(p)}
                  onEdit={() => setEditingProvider(p)}
                />
              ))}
            </div>
          )}
        </>
      ) : (
        /* Tenant-admin landing: explain the scoped surface + a clear entry to
           the default-model picker (which is also auto-opened on mount). */
        <div className="border border-line rounded-lg px-6 py-8 bg-paper shadow-sm flex flex-col items-start gap-3">
          <div className="text-[14px] text-ink font-semibold">Default model preferences</div>
          <p className="text-[12px] text-dim leading-relaxed max-w-[640px]">
            Set default models for every user in this tenant. They apply whenever a user hasn’t set their own preference.
            The provider directory is maintained by platform administrators.
          </p>
          <button
            type="button"
            onClick={() => setShowDefaults(true)}
            className="px-4 py-2 bg-ink text-paper text-[13px] font-mono rounded-md shadow hover:bg-ink-2 transition-colors"
          >
            Configure default models
          </button>
        </div>
      )}

      {/* Disabled overlay indicators (provider mutations → super_admin only) */}
      {canManageProviders && (deleting || toggling) && (
        <div className="fixed bottom-4 right-4 bg-ink text-paper px-3 py-2 rounded-md text-[12px] font-mono shadow-lg">
          {deleting ? 'Deleting…' : 'Updating…'}
        </div>
      )}

      {/* Provider add/edit dialogs — platform super_admin only. */}
      {canManageProviders && (
        <>
          <AddProviderDialog
            open={showAdd}
            onClose={() => setShowAdd(false)}
            onAdded={loadProviders}
          />
          <EditProviderDialog
            open={!!editingProvider}
            provider={editingProvider}
            onClose={() => setEditingProvider(null)}
            onUpdated={loadProviders}
          />
        </>
      )}

      {/* Default models dialog — visible to both platform admins (tenant scope
          they select with the tenant switcher upstream) and tenant_admins (own
          tenant, locked). */}
      {showDefaults && <DefaultModelsDialog onClose={() => setShowDefaults(false)} />}
    </div>
    </div>
  );
}
