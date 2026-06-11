import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import type { DlpClassification, DlpScanResult, DlpSensitivity } from '../../api/types';

const SENSITIVITIES: DlpSensitivity[] = ['L1', 'L2', 'L3', 'L4', 'L5'];

export function DlpPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();

  // Scan tool.
  const [content, setContent] = useState('');
  const [scanRt, setScanRt] = useState('document');
  const [scanRid, setScanRid] = useState('');
  const [scanResult, setScanResult] = useState<DlpScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);

  // Classification.
  const [tagRt, setTagRt] = useState('document');
  const [tagRid, setTagRid] = useState('');
  const [sensitivity, setSensitivity] = useState<DlpSensitivity>('L3');
  const [tags, setTags] = useState('');
  const [reason, setReason] = useState('');
  const [tagging, setTagging] = useState(false);
  const [tagError, setTagError] = useState<string | null>(null);

  const [getRt, setGetRt] = useState('document');
  const [getRid, setGetRid] = useState('');
  const [classification, setClassification] = useState<DlpClassification | null>(null);
  const [getting, setGetting] = useState(false);
  const [getError, setGetError] = useState<string | null>(null);

  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const tid = selectedTenantId ?? undefined;

  const handleScan = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim() || !scanRid.trim()) { setScanError('请填写内容与资源 ID'); return; }
    setScanning(true);
    setScanError(null);
    setScanResult(null);
    try {
      const res = await api.admin.dlp.scan(
        { content, resource_type: scanRt.trim(), resource_id: scanRid.trim() },
        tid,
      );
      setScanResult(res);
    } catch (err) {
      setScanError(err instanceof ApiError ? err.message : '扫描失败');
    } finally {
      setScanning(false);
    }
  };

  const handleTag = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tagRid.trim()) { setTagError('请填写资源 ID'); return; }
    setTagging(true);
    setTagError(null);
    try {
      await api.admin.dlp.tagClassification(
        {
          resource_type: tagRt.trim(),
          resource_id: tagRid.trim(),
          sensitivity,
          tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
          reason: reason.trim() || null,
        },
        tid,
      );
      toast.success('已标注密级');
      setTags(''); setReason('');
    } catch (err) {
      setTagError(err instanceof ApiError ? err.message : '标注失败');
    } finally {
      setTagging(false);
    }
  };

  const handleGet = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!getRid.trim()) { setGetError('请填写资源 ID'); return; }
    setGetting(true);
    setGetError(null);
    setClassification(null);
    try {
      const res = await api.admin.dlp.getClassification(getRt.trim(), getRid.trim(), tid);
      setClassification(res);
    } catch (err) {
      setGetError(err instanceof ApiError ? err.message : '查询失败');
    } finally {
      setGetting(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">数据防泄漏</h1>
        <p className="text-[12px] text-dim mt-0.5">内容扫描与资源密级标注（→ dlp）</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[{ value: '', label: '（默认 / 当前租户）' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {/* Content scan */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">内容扫描</div>
        <form onSubmit={handleScan} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
          {scanError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{scanError}</div>}
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">待扫描内容</label>
            <textarea
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent resize-y min-h-[100px] font-mono"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="粘贴待检测的文本…"
            />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">资源类型</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={scanRt} onChange={(e) => setScanRt(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">资源 ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={scanRid} onChange={(e) => setScanRid(e.target.value)} placeholder="doc-123" />
            </div>
          </div>
          <div>
            <button type="submit" disabled={scanning} className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50">{scanning ? '扫描中…' : '开始扫描'}</button>
          </div>
        </form>
        {scanResult && (
          <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">扫描结果</div>
            <pre className="text-[11px] font-mono text-ink-2 whitespace-pre-wrap break-all bg-paper-2 rounded-sm p-2 overflow-auto max-h-[280px]">
              {JSON.stringify(scanResult, null, 2)}
            </pre>
          </div>
        )}
      </section>

      {/* Classification tag */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源密级标注</div>
        <form onSubmit={handleTag} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
          {tagError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{tagError}</div>}
          <div className="grid grid-cols-3 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">资源类型</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={tagRt} onChange={(e) => setTagRt(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">资源 ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={tagRid} onChange={(e) => setTagRid(e.target.value)} placeholder="doc-123" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">密级</label>
              <CustomSelect
                options={SENSITIVITIES.map((s) => ({ value: s, label: s }))}
                value={sensitivity}
                onChange={(v) => setSensitivity(v as DlpSensitivity)}
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">标签 (逗号分隔, 可选)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="pii, finance" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">原因 (可选)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
          </div>
          <div>
            <button type="submit" disabled={tagging} className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50">{tagging ? '标注中…' : '标注密级'}</button>
          </div>
        </form>

        {/* Query classification */}
        <form onSubmit={handleGet} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
          <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">查询密级</div>
          {getError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{getError}</div>}
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">资源类型</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={getRt} onChange={(e) => setGetRt(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">资源 ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={getRid} onChange={(e) => setGetRid(e.target.value)} placeholder="doc-123" />
            </div>
          </div>
          <div>
            <button type="submit" disabled={getting} className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50">{getting ? '查询中…' : '查询'}</button>
          </div>
          {classification && (
            <div className="border-t border-line-2 pt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px]">
              <span className="font-mono text-dim">密级</span>
              <span className="font-mono text-ink-2">{classification.sensitivity ?? '—'}</span>
              <span className="font-mono text-dim">标签</span>
              <span className="font-mono text-ink-2">{classification.tags?.join(', ') || '—'}</span>
              <span className="font-mono text-dim">可降级</span>
              <span className="font-mono text-ink-2">{classification.downgradable === undefined ? '—' : classification.downgradable ? '是' : '否'}</span>
            </div>
          )}
        </form>
      </section>
    </div>
  );
}
