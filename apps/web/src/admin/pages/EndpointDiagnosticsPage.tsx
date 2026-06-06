import { useState } from 'react';
import { api, ApiError } from '../../api/client';
import type { ProbeCapabilityResult, ProbeReport, ProviderType } from '../../api/types';

// ─── Capability metadata ──────────────────────────────────────────────────────

/** Only the required/boolean keys from ProbeResults (excludes optional model_count). */
type ProbeCapabilityKey = 'list_models' | 'function_calling' | 'streaming' | 'context_window_32k' | 'parallel_tool_calls';

interface CapabilitySpec {
  key: ProbeCapabilityKey;
  label: string;
  /** When false/not met, what agent behaviour degrades */
  degradeNote: string;
  required: boolean;
}

const CAPABILITIES: CapabilitySpec[] = [
  {
    key: 'list_models',
    label: '模型列表 (GET /v1/models)',
    degradeNote: '无法枚举可用模型,需手动指定模型 ID',
    required: true,
  },
  {
    key: 'function_calling',
    label: 'Function Calling (工具调用)',
    degradeNote: 'Agent 无法使用任何工具,完全降级为纯对话模式',
    required: true,
  },
  {
    key: 'streaming',
    label: '流式输出 (SSE)',
    degradeNote: '前端无法逐 token 展示,等待完整响应后才显示',
    required: true,
  },
  {
    key: 'context_window_32k',
    label: '上下文窗口 ≥ 32K',
    degradeNote: '复杂任务上下文截断,长对话/大文件分析受限',
    required: true,
  },
  {
    key: 'parallel_tool_calls',
    label: '并行工具调用',
    degradeNote: '多工具请求降级为串行执行,速度下降',
    required: false,
  },
];

// ─── Helpers ─────────────────────────────────────────────────────────────────

function isPass(val: ProbeCapabilityResult): boolean {
  return val === true;
}

function isFail(val: ProbeCapabilityResult): boolean {
  return val === false || (typeof val === 'string' && val.startsWith('FAILED'));
}

function isUnknown(val: ProbeCapabilityResult): boolean {
  return val === 'unknown' || (typeof val === 'string' && !val.startsWith('FAILED') && val !== 'true' && val !== 'false');
}

function StatusBadge({ val }: { val: ProbeCapabilityResult }) {
  if (isPass(val)) {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[11px] text-green-700 bg-green-50 border border-green-200 rounded-sm px-1.5 py-0.5">
        ✓ 通过
      </span>
    );
  }
  if (isFail(val)) {
    return (
      <span className="inline-flex items-center gap-1 font-mono text-[11px] text-red-700 bg-red-50 border border-red-200 rounded-sm px-1.5 py-0.5">
        ✗ 不支持
      </span>
    );
  }
  // unknown or intermediate string
  return (
    <span className="inline-flex items-center gap-1 font-mono text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-sm px-1.5 py-0.5">
      ? 未知
    </span>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

const PROVIDER_OPTIONS: { value: ProviderType; label: string }[] = [
  { value: 'openai_compatible', label: 'OpenAI 兼容 (默认)' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'vllm', label: 'vLLM' },
  { value: 'ollama', label: 'Ollama' },
  { value: 'bedrock', label: 'AWS Bedrock' },
  { value: 'azure', label: 'Azure OpenAI' },
];

export function EndpointDiagnosticsPage() {
  const [url, setUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [providerType, setProviderType] = useState<ProviderType>('openai_compatible');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ProbeReport | null>(null);

  const handleProbe = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim() || !apiKey.trim()) return;
    setLoading(true);
    setError(null);
    setReport(null);
    try {
      const data = await api.admin.llm.probe({
        url: url.trim(),
        api_key: apiKey,
        provider_type: providerType,
      });
      setReport(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '探测失败,请检查端点地址和密钥');
    } finally {
      setLoading(false);
    }
  };

  // Derive summary pass/fail counts
  const passCount = report
    ? CAPABILITIES.filter((c) => isPass(report.results[c.key])).length
    : 0;
  const failRequired = report
    ? CAPABILITIES.filter((c) => c.required && isFail(report.results[c.key]))
    : [];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      {/* Header */}
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">端点诊断</h1>
        <p className="text-[12px] text-dim mt-0.5">
          探测 LLM 端点的能力兼容性,输出 Agent 可用性报告
        </p>
      </div>

      {/* Probe form */}
      <form
        onSubmit={handleProbe}
        className="border border-line rounded-md p-4 bg-surface flex flex-col gap-4"
      >
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">端点配置</div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] font-mono text-dim">端点 Base URL</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
            placeholder="如 http://my-llm-service:8080"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] font-mono text-dim">
            API Key
            <span className="ml-1.5 text-[10px] text-dim font-normal normal-case tracking-normal">
              (仅用于探测,不存储)
            </span>
          </label>
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

        <div className="flex flex-col gap-1">
          <label className="text-[11px] font-mono text-dim">Provider 类型</label>
          <select
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={providerType}
            onChange={(e) => setProviderType(e.target.value as ProviderType)}
          >
            {PROVIDER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <button
            type="submit"
            disabled={loading || !url.trim() || !apiKey.trim()}
            className="px-3 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? '探测中…' : '测试端点'}
          </button>
        </div>
      </form>

      {/* Error */}
      {error && (
        <div className="border border-red-200 rounded-md px-4 py-3 bg-red-50 text-[13px] text-red-700">
          {error}
        </div>
      )}

      {/* Report */}
      {report && (
        <div className="flex flex-col gap-4">
          {/* Summary banner */}
          <div
            className={[
              'border rounded-md px-4 py-3 flex items-center gap-3',
              failRequired.length === 0
                ? 'border-green-200 bg-green-50'
                : 'border-red-200 bg-red-50',
            ].join(' ')}
          >
            <span className="text-[20px]">{failRequired.length === 0 ? '✓' : '✗'}</span>
            <div>
              <div
                className={[
                  'text-[13px] font-semibold',
                  failRequired.length === 0 ? 'text-green-800' : 'text-red-800',
                ].join(' ')}
              >
                {failRequired.length === 0
                  ? `全部必选能力通过 (${passCount}/${CAPABILITIES.length})`
                  : `${failRequired.length} 项必选能力不满足`}
              </div>
              <div className="font-mono text-[10.5px] text-dim mt-0.5">
                {report.url} · {report.provider_type}
                {report.results.model_count ? ` · ${report.results.model_count} 个模型` : ''}
              </div>
            </div>
          </div>

          {/* Capability table */}
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">能力</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">结果</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">必选</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">不满足的影响</th>
                </tr>
              </thead>
              <tbody>
                {CAPABILITIES.map((cap) => {
                  const val = report.results[cap.key];
                  const failing = isFail(val);
                  const isCtxWindow = cap.key === 'context_window_32k';
                  return (
                    <tr
                      key={cap.key}
                      className={[
                        'border-b border-line-2',
                        failing && cap.required ? 'bg-red-50/50' : 'hover:bg-paper-2',
                      ].join(' ')}
                    >
                      <td className="px-3 py-2 text-[12px] text-ink">
                        {cap.label}
                        {isCtxWindow && report.results.context_window_value !== 'unknown' && (
                          <span className="ml-2 font-mono text-[10.5px] text-dim">
                            ({Number(report.results.context_window_value).toLocaleString()} tokens)
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <StatusBadge val={val} />
                      </td>
                      <td className="px-3 py-2 font-mono text-[11px] text-dim">
                        {cap.required ? (
                          <span className="text-ink-2">必选</span>
                        ) : (
                          <span>可选</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-[11px] text-dim">
                        {(failing || isUnknown(val)) ? cap.degradeNote : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Raw results (collapsible, for debugging) */}
          <details className="border border-line rounded-md overflow-hidden">
            <summary className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim cursor-pointer bg-paper-2 hover:bg-paper select-none">
              原始探测结果 (JSON)
            </summary>
            <pre className="px-3 py-3 text-[11px] font-mono text-ink-2 overflow-auto bg-paper">
              {JSON.stringify(report.results, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}
