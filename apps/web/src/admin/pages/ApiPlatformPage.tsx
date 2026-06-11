import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect, MultiChipSelect } from '../components/CustomSelect';
import type { OAuthApp, PlatformApiKey, Webhook } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

type Tab = 'keys' | 'oauth' | 'webhooks';

// ─── API keys ─────────────────────────────────────────────────────────────────

function ApiKeysTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [keys, setKeys] = useState<PlatformApiKey[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [rpm, setRpm] = useState('60');
  const [busy, setBusy] = useState(false);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.apiPlatform.keys
      .list(tenantId ?? undefined)
      .then(setKeys)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.admin.apiPlatform.keys.create(
        { name: name.trim(), scopes, rate_limit_rpm: Number(rpm) || 60 },
        tenantId ?? undefined,
      );
      if (created.api_key) setCreatedSecret(created.api_key);
      toast.success('已创建 API Key');
      setName(''); setScopes([]); setRpm('60'); setCreating(false);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  const handleRevoke = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: '吊销 API Key', message: '确定吊销该 Key？吊销后立即失效。', tone: 'danger', confirmText: '吊销' }))) return;
    try {
      await api.admin.apiPlatform.keys.revoke(id, tenantId ?? undefined);
      toast.success('已吊销');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '吊销失败');
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">API Keys</div>
        <button onClick={() => setCreating(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建 Key</button>
      </div>

      {createdSecret && (
        <div className="border border-green-200 bg-green-50 rounded-md p-3 text-[12px] flex flex-col gap-1">
          <div className="text-green-800">请立即复制并妥善保存此 API Key，关闭后将无法再次查看：</div>
          <code className="font-mono text-[12px] text-ink break-all">{createdSecret}</code>
          <button onClick={() => setCreatedSecret(null)} className="self-start mt-1 text-[11px] font-mono text-dim hover:text-ink">我已保存</button>
        </div>
      )}

      {creating && (
        <Dialog title="新建 API Key" onClose={() => { setCreating(false); setError(null); }} dismissable={!busy}>
          <form onSubmit={handleCreate} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">名称</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={name} onChange={(e) => setName(e.target.value)} placeholder="ci-pipeline" />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">权限范围</label>
                <MultiChipSelect options={['read', 'write', 'admin']} value={scopes} onChange={setScopes} />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">速率上限 (rpm)</label>
                <input type="number" className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={rpm} onChange={(e) => setRpm(e.target.value)} />
              </div>
            </div>
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => { setCreating(false); setError(null); }} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
              <button type="submit" disabled={busy || !name.trim()} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{busy ? '创建中…' : '创建'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：API 平台端点不可达。</div>}
      {!loading && !todo && keys.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无 API Key</div>}
      {!loading && !todo && keys.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">名称</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">范围</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">速率</th>
                <th className="px-3 py-2 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k, i) => (
                <tr key={k.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                  <td className="px-3 py-2 text-[13px] text-ink">{k.name ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{k.scopes?.join(', ') || '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{k.rate_limit_rpm ?? '—'}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => handleRevoke(k.id)} className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-accent hover:opacity-80 transition-opacity">吊销</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── OAuth apps ───────────────────────────────────────────────────────────────

function OAuthAppsTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const [apps, setApps] = useState<OAuthApp[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [redirects, setRedirects] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.apiPlatform.oauthApps
      .list(tenantId ?? undefined)
      .then(setApps)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.apiPlatform.oauthApps.create(
        { name: name.trim(), redirect_uris: redirects.split(',').map((s) => s.trim()).filter(Boolean), scopes },
        tenantId ?? undefined,
      );
      toast.success('已注册 OAuth 应用');
      setName(''); setRedirects(''); setScopes([]); setCreating(false);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '注册失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">OAuth 应用</div>
        <button onClick={() => setCreating(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 注册应用</button>
      </div>

      {creating && (
        <Dialog title="注册 OAuth 应用" onClose={() => { setCreating(false); setError(null); }} dismissable={!busy}>
          <form onSubmit={handleCreate} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">名称</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={name} onChange={(e) => setName(e.target.value)} placeholder="acme-portal" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">回调地址 (逗号分隔)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={redirects} onChange={(e) => setRedirects(e.target.value)} placeholder="https://app.acme.co/callback" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">权限范围</label>
              <MultiChipSelect options={['openid', 'profile', 'email', 'read', 'write']} value={scopes} onChange={setScopes} />
            </div>
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => { setCreating(false); setError(null); }} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
              <button type="submit" disabled={busy || !name.trim()} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{busy ? '注册中…' : '注册'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：API 平台端点不可达。</div>}
      {!loading && !todo && apps.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无 OAuth 应用</div>}
      {!loading && !todo && apps.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">名称</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Client ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">范围</th>
              </tr>
            </thead>
            <tbody>
              {apps.map((a, i) => (
                <tr key={a.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 text-[13px] text-ink">{a.name ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[200px]">{a.client_id ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{a.scopes?.join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Webhooks ─────────────────────────────────────────────────────────────────

function WebhooksTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const confirm = useConfirm();
  const [hooks, setHooks] = useState<Webhook[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [url, setUrl] = useState('');
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.apiPlatform.webhooks
      .list(tenantId ?? undefined)
      .then(setHooks)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  useEffect(() => { load(); }, [load]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.apiPlatform.webhooks.create(
        { url: url.trim(), event_types: eventTypes, secret: secret.trim() || null },
        tenantId ?? undefined,
      );
      toast.success('已订阅 Webhook');
      setUrl(''); setEventTypes([]); setSecret(''); setCreating(false);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '订阅失败');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: '删除 Webhook', message: '确定删除该 Webhook 订阅？', tone: 'danger', confirmText: '删除' }))) return;
    try {
      await api.admin.apiPlatform.webhooks.delete(id, tenantId ?? undefined);
      toast.success('已删除');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Webhooks</div>
        <button onClick={() => setCreating(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 订阅 Webhook</button>
      </div>

      {creating && (
        <Dialog title="订阅 Webhook" onClose={() => { setCreating(false); setError(null); }} dismissable={!busy}>
          <form onSubmit={handleCreate} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">URL</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://hooks.acme.co/events" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">事件类型</label>
              <MultiChipSelect options={['run.completed', 'run.failed', 'user.created', 'user.deleted', 'session.created']} value={eventTypes} onChange={setEventTypes} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">签名密钥 (可选)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={secret} onChange={(e) => setSecret(e.target.value)} />
            </div>
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => { setCreating(false); setError(null); }} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
              <button type="submit" disabled={busy || !url.trim()} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{busy ? '订阅中…' : '订阅'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：API 平台端点不可达。</div>}
      {!loading && !todo && hooks.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无 Webhook</div>}
      {!loading && !todo && hooks.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">URL</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">事件类型</th>
                <th className="px-3 py-2 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {hooks.map((h, i) => (
                <tr key={h.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                  <td className="px-3 py-2 font-mono text-[12px] text-ink-2 truncate max-w-[260px]">{h.url ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{h.event_types?.join(', ') || '—'}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => handleDelete(h.id)} className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-accent hover:opacity-80 transition-opacity">删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function ApiPlatformPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const [tab, setTab] = useState<Tab>('keys');

  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const tabs: { id: Tab; label: string }[] = [
    { id: 'keys', label: 'API Keys' },
    { id: 'oauth', label: 'OAuth 应用' },
    { id: 'webhooks', label: 'Webhooks' },
  ];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">API 平台</h1>
        <p className="text-[12px] text-dim mt-0.5">API Key、OAuth 应用与 Webhook 管理（→ api-platform）</p>
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

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-line-2">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 text-[12px] font-mono border-b-2 -mb-px transition-colors ${
              tab === t.id ? 'border-accent text-ink' : 'border-transparent text-dim hover:text-ink'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'keys' && <ApiKeysTab tenantId={selectedTenantId} />}
      {tab === 'oauth' && <OAuthAppsTab tenantId={selectedTenantId} />}
      {tab === 'webhooks' && <WebhooksTab tenantId={selectedTenantId} />}
    </div>
  );
}
