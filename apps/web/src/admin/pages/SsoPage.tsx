import { Fragment, useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type {
  SsoProvider,
  SsoKind,
  SsoScope,
  SsoMatchType,
  SsoRoutingRule,
  CreateSsoRoutingRuleRequest,
} from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

const KIND_LABELS: Record<SsoKind, string> = {
  OIDC: 'OpenID Connect',
  SAML: 'SAML 2.0',
  AD: 'Azure AD / Entra',
  LDAP: 'LDAP / AD',
  DINGTALK_ENTERPRISE: '钉钉',
  FEISHU_ENTERPRISE: '飞书',
  WECOM: '企业微信',
};

const KIND_OPTIONS = (Object.keys(KIND_LABELS) as SsoKind[]).map((k) => ({
  value: k,
  label: KIND_LABELS[k],
}));

const MATCH_TYPE_LABELS: Record<SsoMatchType, string> = {
  EMAIL_DOMAIN: '邮箱域',
  EQUALS: '等于',
  CONTAINS: '包含',
  REGEX: '正则',
};

const MATCH_TYPE_OPTIONS = (Object.keys(MATCH_TYPE_LABELS) as SsoMatchType[]).map((k) => ({
  value: k,
  label: MATCH_TYPE_LABELS[k],
}));

// ─── Config form fields per kind ─────────────────────────────────────────────

interface FieldSpec {
  key: string;
  label: string;
  placeholder?: string;
  type?: 'text' | 'password' | 'textarea';
}

const CONFIG_FIELDS: Record<SsoKind, FieldSpec[]> = {
  SAML: [
    { key: 'sso_url', label: 'SSO URL', placeholder: 'https://idp.example.com/sso' },
    { key: 'entity_id', label: 'Entity ID', placeholder: 'https://idp.example.com/metadata' },
    { key: 'certificate', label: '证书', placeholder: '-----BEGIN CERTIFICATE-----', type: 'textarea' },
  ],
  OIDC: [
    { key: 'discovery_url', label: 'Discovery URL', placeholder: 'https://accounts.google.com/.well-known/openid-configuration' },
    { key: 'client_id', label: 'Client ID' },
    { key: 'client_secret', label: 'Client Secret', type: 'password' },
  ],
  AD: [
    { key: 'azure_tenant_id', label: 'Azure AD Tenant ID', placeholder: 'Azure AD 租户 ID' },
    { key: 'client_id', label: 'Client ID' },
    { key: 'client_secret', label: 'Client Secret', type: 'password' },
  ],
  LDAP: [
    { key: 'server_url', label: '服务器地址', placeholder: 'ldap://ad.corp.com:389' },
    { key: 'base_dn', label: 'Base DN', placeholder: 'dc=corp,dc=com' },
    { key: 'bind_dn', label: 'Bind DN', placeholder: 'cn=admin,dc=corp,dc=com' },
    { key: 'bind_password', label: 'Bind 密码', type: 'password' },
    { key: 'user_search_filter', label: '用户搜索过滤', placeholder: '(objectClass=person)' },
  ],
  DINGTALK_ENTERPRISE: [
    { key: 'corp_id', label: 'Corp ID' },
    { key: 'app_key', label: 'App Key' },
    { key: 'app_secret', label: 'App Secret', type: 'password' },
  ],
  FEISHU_ENTERPRISE: [
    { key: 'app_id', label: 'App ID' },
    { key: 'app_secret', label: 'App Secret', type: 'password' },
  ],
  WECOM: [
    { key: 'corp_id', label: 'Corp ID' },
    { key: 'agent_id', label: 'Agent ID' },
    { key: 'secret', label: 'Secret', type: 'password' },
  ],
};

function ConfigFields({
  kind,
  config,
  setField,
}: {
  kind: SsoKind;
  config: Record<string, string>;
  setField: (key: string, value: string) => void;
}) {
  const fields = CONFIG_FIELDS[kind];
  return (
    <div className="border-t border-line-2 pt-2 flex flex-col gap-2">
      <div className="text-[10.5px] font-mono text-dim uppercase tracking-[0.1em]">
        {KIND_LABELS[kind]} 配置
      </div>
      {fields.map((f) =>
        f.type === 'textarea' ? (
          <div key={f.key} className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">{f.label}</label>
            <textarea
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono resize-y min-h-[60px]"
              value={config[f.key] ?? ''}
              onChange={(e) => setField(f.key, e.target.value)}
              placeholder={f.placeholder}
              rows={3}
            />
          </div>
        ) : (
          <div key={f.key} className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">{f.label}</label>
            <input
              type={f.type ?? 'text'}
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
              value={config[f.key] ?? ''}
              onChange={(e) => setField(f.key, e.target.value)}
              placeholder={f.placeholder}
            />
          </div>
        ),
      )}
    </div>
  );
}

// ─── Tenant-scoped create dialog (existing) ──────────────────────────────────

function CreateSsoDialog({
  tenantId,
  onClose,
  onCreated,
}: {
  tenantId: string | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const toast = useToast();
  const [kind, setKind] = useState<SsoKind>('SAML');
  const [name, setName] = useState('');
  const [config, setConfig] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setField = (key: string, value: string) =>
    setConfig((prev) => ({ ...prev, [key]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.sso.create(
        { kind, name: name.trim(), config },
        tenantId ?? undefined,
      );
      toast.success('身份源已创建');
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="添加身份源" onClose={onClose} dismissable={!busy}>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">类型</label>
          <CustomSelect
            options={KIND_OPTIONS}
            value={kind}
            onChange={(v) => { setKind(v as SsoKind); setConfig({}); }}
            className="w-full"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">名称</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：公司域账号"
          />
        </div>

        <ConfigFields kind={kind} config={config} setField={setField} />

        {error && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? '创建中…' : '创建'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ─── Tenant-scoped tab (existing page content) ───────────────────────────────

function TenantSsoTab() {
  const { tenants, tenantsLoading, selectedTenantId, setSelectedTenantId } = useAdminStore();
  const toast = useToast();
  const confirm = useConfirm();

  const [providers, setProviders] = useState<SsoProvider[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.sso
      .list(selectedTenantId ?? undefined)
      .then(setProviders)
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => setLoading(false));
  }, [selectedTenantId]);

  useEffect(() => { load(); }, [load]);

  const handleToggle = async (p: SsoProvider) => {
    if (p.enabled) {
      const yes = await confirm({
        title: '禁用身份源',
        message: `禁用「${p.name}」后，使用该身份源登录的用户将无法登录。确定继续？`,
        tone: 'danger',
        confirmText: '禁用',
      });
      if (!yes) return;
      try {
        await api.admin.sso.disable(p.id, selectedTenantId ?? undefined);
        toast.success('已禁用');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : '操作失败');
      }
    } else {
      try {
        await api.admin.sso.enable(p.id, selectedTenantId ?? undefined);
        toast.success('已启用');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : '操作失败');
      }
    }
  };

  const handleDelete = async (p: SsoProvider) => {
    const yes = await confirm({
      title: '删除身份源',
      message: `删除后「${p.name}」的配置将永久移除，关联用户需重新绑定。确定继续？`,
      tone: 'danger',
      confirmText: '删除',
    });
    if (!yes) return;
    try {
      await api.admin.sso.delete(p.id, selectedTenantId ?? undefined);
      toast.success('已删除');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
    }
  };

  return (
    <div className="flex flex-col gap-5">
      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[
            { value: '', label: '（默认 / 当前租户）' },
            ...tenants.map((t) => ({ value: t.id, label: t.name })),
          ]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {/* Header + action */}
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">身份源列表</div>
        <button
          onClick={() => setCreating(true)}
          className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
        >
          + 添加身份源
        </button>
      </div>

      {creating && (
        <CreateSsoDialog
          tenantId={selectedTenantId}
          onClose={() => setCreating(false)}
          onCreated={() => { setCreating(false); load(); }}
        />
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力暂未启用：身份源管理端点不可达。
        </div>
      )}
      {!loading && !todo && providers.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">暂无身份源配置</div>
      )}

      {!loading && !todo && providers.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">名称</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">创建时间</th>
                <th className="px-3 py-2 w-28"></th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p, i) => (
                <tr key={p.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                  <td className="px-3 py-2 text-[13px] text-ink">{p.name}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{KIND_LABELS[p.kind] ?? p.kind}</td>
                  <td className="px-3 py-2 text-[12px]">
                    {p.enabled ? (
                      <span className="text-green-700">已启用</span>
                    ) : (
                      <span className="text-dim">已禁用</span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {p.created_at ? new Date(p.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td className="px-3 py-2 flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => handleToggle(p)}
                      className="text-[11px] font-mono text-ink-2 hover:text-ink"
                    >
                      {p.enabled ? '禁用' : '启用'}
                    </button>
                    <button
                      onClick={() => handleDelete(p)}
                      className="text-[11px] font-mono text-accent hover:opacity-80"
                    >
                      删除
                    </button>
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

// ─── Platform-scoped create dialog ───────────────────────────────────────────

function CreatePlatformSsoDialog({
  tenantOptions,
  onClose,
  onCreated,
}: {
  tenantOptions: { value: string; label: string }[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const toast = useToast();
  const [kind, setKind] = useState<SsoKind>('SAML');
  const [name, setName] = useState('');
  const [config, setConfig] = useState<Record<string, string>>({});
  const [scope, setScope] = useState<SsoScope>('PLATFORM');
  const [tenantId, setTenantId] = useState('');
  const [defaultTenantId, setDefaultTenantId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setField = (key: string, value: string) =>
    setConfig((prev) => ({ ...prev, [key]: value }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    if (scope === 'TENANT' && !tenantId) {
      setError('租户级身份源必须选择目标租户');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.admin.sso.platform.create({
        kind,
        name: name.trim(),
        config,
        scope,
        tenant_id: scope === 'TENANT' ? tenantId : null,
        default_tenant_id: scope === 'PLATFORM' ? (defaultTenantId || null) : null,
      });
      toast.success('身份源已创建');
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="添加平台身份源" onClose={onClose} dismissable={!busy}>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">类型</label>
          <CustomSelect
            options={KIND_OPTIONS}
            value={kind}
            onChange={(v) => { setKind(v as SsoKind); setConfig({}); }}
            className="w-full"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">名称</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：公司域账号"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">范围</label>
          <CustomSelect
            options={[
              { value: 'PLATFORM', label: '平台级' },
              { value: 'TENANT', label: '租户级' },
            ]}
            value={scope}
            onChange={(v) => setScope(v as SsoScope)}
            className="w-full"
          />
        </div>
        {scope === 'TENANT' ? (
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">目标租户</label>
            <CustomSelect
              options={[{ value: '', label: '（选择租户）' }, ...tenantOptions]}
              value={tenantId}
              onChange={setTenantId}
              className="w-full"
            />
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">默认租户</label>
            <CustomSelect
              options={[{ value: '', label: '（无）' }, ...tenantOptions]}
              value={defaultTenantId}
              onChange={setDefaultTenantId}
              className="w-full"
            />
            <span className="text-[10.5px] text-dim">无匹配规则时回落的租户</span>
          </div>
        )}

        <ConfigFields kind={kind} config={config} setField={setField} />

        {error && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
        )}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? '创建中…' : '创建'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ─── Routing-rules panel ─────────────────────────────────────────────────────

function RoutingRulesPanel({
  provider,
  tenantOptions,
  tenantNameOf,
}: {
  provider: SsoProvider;
  tenantOptions: { value: string; label: string }[];
  tenantNameOf: (id: string | null | undefined) => string;
}) {
  const toast = useToast();
  const confirm = useConfirm();

  const [rules, setRules] = useState<SsoRoutingRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Add-rule form state
  const [priority, setPriority] = useState('100');
  const [matchType, setMatchType] = useState<SsoMatchType>('EMAIL_DOMAIN');
  const [matchAttribute, setMatchAttribute] = useState('email');
  const [matchValue, setMatchValue] = useState('');
  const [targetTenantId, setTargetTenantId] = useState('');
  const [defaultRole, setDefaultRole] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.sso.platform.rules
      .list(provider.id)
      .then((rs) => setRules([...rs].sort((a, b) => a.priority - b.priority)))
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => setLoading(false));
  }, [provider.id]);

  useEffect(() => { load(); }, [load]);

  // EMAIL_DOMAIN matches against the email; force the attribute accordingly.
  const onMatchTypeChange = (v: string) => {
    const next = v as SsoMatchType;
    setMatchType(next);
    if (next === 'EMAIL_DOMAIN') setMatchAttribute('email');
  };

  const valuePlaceholder =
    matchType === 'EMAIL_DOMAIN' ? 'acme.com' : matchType === 'REGEX' ? '^dev-.*' : 'value';

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!matchValue.trim() || !targetTenantId) {
      setError('匹配值与目标租户必填');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body: CreateSsoRoutingRuleRequest = {
        priority: Number(priority) || 100,
        match_type: matchType,
        match_attribute: matchType === 'EMAIL_DOMAIN' ? 'email' : matchAttribute.trim(),
        match_value: matchValue.trim(),
        target_tenant_id: targetTenantId,
        default_role: defaultRole.trim() || null,
        enabled: true,
      };
      await api.admin.sso.platform.rules.create(provider.id, body);
      toast.success('规则已添加');
      setPriority('100');
      setMatchType('EMAIL_DOMAIN');
      setMatchAttribute('email');
      setMatchValue('');
      setTargetTenantId('');
      setDefaultRole('');
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '添加失败');
    } finally {
      setBusy(false);
    }
  };

  const handleToggleEnabled = async (r: SsoRoutingRule) => {
    try {
      await api.admin.sso.platform.rules.update(provider.id, r.id, { enabled: !r.enabled });
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '操作失败');
    }
  };

  const handleDeleteRule = async (r: SsoRoutingRule) => {
    const yes = await confirm({
      title: '删除路由规则',
      message: '删除后该匹配规则将不再生效。确定继续？',
      tone: 'danger',
      confirmText: '删除',
    });
    if (!yes) return;
    try {
      await api.admin.sso.platform.rules.delete(provider.id, r.id);
      toast.success('已删除');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
    }
  };

  return (
    <div className="flex flex-col gap-3 bg-paper-2 border border-line-2 rounded-md p-4">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
        「{provider.name}」路由规则
      </div>

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && (
        <div className="border border-line rounded-md p-3 text-[12px] text-dim italic">
          该能力暂未启用：路由规则端点不可达。
        </div>
      )}

      {!loading && !todo && rules.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">暂无路由规则</div>
      )}

      {!loading && !todo && rules.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden bg-paper">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">优先级</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">匹配属性</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">匹配方式</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">匹配值</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">目标租户</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">默认角色</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 w-16"></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                  <td className="px-3 py-2 font-mono text-[12px] text-ink-2">{r.priority}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{r.match_attribute}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{MATCH_TYPE_LABELS[r.match_type] ?? r.match_type}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[180px]">{r.match_value}</td>
                  <td className="px-3 py-2 text-[12px] text-ink">{tenantNameOf(r.target_tenant_id)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{r.default_role || '—'}</td>
                  <td className="px-3 py-2 text-[12px]">
                    <button
                      onClick={() => handleToggleEnabled(r)}
                      className={r.enabled ? 'text-green-700 hover:opacity-80' : 'text-dim hover:text-ink'}
                    >
                      {r.enabled ? '已启用' : '已禁用'}
                    </button>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => handleDeleteRule(r)}
                      className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-accent hover:opacity-80 transition-opacity"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Add rule form */}
      {!todo && (
        <form onSubmit={handleAdd} className="flex flex-col gap-2 border-t border-line-2 pt-3">
          <div className="text-[10.5px] font-mono text-dim uppercase tracking-[0.1em]">添加规则</div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">优先级</label>
              <input
                type="number"
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">匹配方式</label>
              <CustomSelect
                options={MATCH_TYPE_OPTIONS}
                value={matchType}
                onChange={onMatchTypeChange}
                className="w-full"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">匹配属性</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono disabled:opacity-50"
                value={matchAttribute}
                onChange={(e) => setMatchAttribute(e.target.value)}
                placeholder="department / groups / email"
                disabled={matchType === 'EMAIL_DOMAIN'}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">匹配值</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={matchValue}
                onChange={(e) => setMatchValue(e.target.value)}
                placeholder={valuePlaceholder}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">目标租户</label>
              <CustomSelect
                options={[{ value: '', label: '（选择租户）' }, ...tenantOptions]}
                value={targetTenantId}
                onChange={setTargetTenantId}
                className="w-full"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">默认角色（可选）</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={defaultRole}
                onChange={(e) => setDefaultRole(e.target.value)}
                placeholder="member"
              />
            </div>
          </div>
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={busy}
              className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
            >
              {busy ? '添加中…' : '添加规则'}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

// ─── Platform-scoped tab ─────────────────────────────────────────────────────

function PlatformSsoTab() {
  const { tenants } = useAdminStore();
  const toast = useToast();
  const confirm = useConfirm();

  const [providers, setProviders] = useState<SsoProvider[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const tenantOptions = tenants.map((t) => ({ value: t.id, label: t.name }));
  const tenantNameOf = (id: string | null | undefined) =>
    id ? (tenants.find((t) => t.id === id)?.name ?? id) : '—';

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.sso.platform
      .list()
      .then(setProviders)
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleToggle = async (p: SsoProvider) => {
    if (p.enabled) {
      const yes = await confirm({
        title: '禁用身份源',
        message: `禁用「${p.name}」后，使用该身份源登录的用户将无法登录。确定继续？`,
        tone: 'danger',
        confirmText: '禁用',
      });
      if (!yes) return;
      try {
        await api.admin.sso.platform.disable(p.id);
        toast.success('已禁用');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : '操作失败');
      }
    } else {
      try {
        await api.admin.sso.platform.enable(p.id);
        toast.success('已启用');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : '操作失败');
      }
    }
  };

  const handleDelete = async (p: SsoProvider) => {
    const yes = await confirm({
      title: '删除身份源',
      message: `删除后「${p.name}」的配置将永久移除，关联路由规则一并删除。确定继续？`,
      tone: 'danger',
      confirmText: '删除',
    });
    if (!yes) return;
    try {
      await api.admin.sso.platform.delete(p.id);
      toast.success('已删除');
      if (expandedId === p.id) setExpandedId(null);
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
    }
  };

  const scopeLabel = (p: SsoProvider) =>
    p.scope === 'TENANT' ? `租户级：${tenantNameOf(p.tenant_id)}` : '平台级';

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">平台身份源列表</div>
        <button
          onClick={() => setCreating(true)}
          className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
        >
          + 添加身份源
        </button>
      </div>

      {creating && (
        <CreatePlatformSsoDialog
          tenantOptions={tenantOptions}
          onClose={() => setCreating(false)}
          onCreated={() => { setCreating(false); load(); }}
        />
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力暂未启用：平台身份源端点不可达。
        </div>
      )}
      {!loading && !todo && providers.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">暂无平台身份源配置</div>
      )}

      {!loading && !todo && providers.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">名称</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">范围</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">默认租户</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 w-56"></th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p, i) => (
                <Fragment key={p.id ?? i}>
                  <tr className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                    <td className="px-3 py-2 text-[13px] text-ink">{p.name}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">{KIND_LABELS[p.kind] ?? p.kind}</td>
                    <td className="px-3 py-2 text-[12px] text-ink-2">{scopeLabel(p)}</td>
                    <td className="px-3 py-2 text-[12px] text-ink-2">{tenantNameOf(p.default_tenant_id)}</td>
                    <td className="px-3 py-2 text-[12px]">
                      {p.enabled ? (
                        <span className="text-green-700">已启用</span>
                      ) : (
                        <span className="text-dim">已禁用</span>
                      )}
                    </td>
                    <td className="px-3 py-2 flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        onClick={() => handleToggle(p)}
                        className="text-[11px] font-mono text-ink-2 hover:text-ink"
                      >
                        {p.enabled ? '禁用' : '启用'}
                      </button>
                      <button
                        onClick={() => setExpandedId((id) => (id === p.id ? null : p.id))}
                        className="text-[11px] font-mono text-ink-2 hover:text-ink"
                      >
                        路由规则
                      </button>
                      <button
                        onClick={() => handleDelete(p)}
                        className="text-[11px] font-mono text-accent hover:opacity-80"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                  {expandedId === p.id && (
                    <tr className="border-b border-line-2">
                      <td colSpan={6} className="p-3 bg-paper">
                        <RoutingRulesPanel
                          provider={p}
                          tenantOptions={tenantOptions}
                          tenantNameOf={tenantNameOf}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

type Tab = 'platform' | 'tenant';

export function SsoPage() {
  const { tenants, setTenants, setTenantsLoading } = useAdminStore();
  const [tab, setTab] = useState<Tab>('platform');

  // Load tenants (shared by both tabs)
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
    { id: 'platform', label: '平台身份源' },
    { id: 'tenant', label: '租户身份源' },
  ];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">身份源管理</h1>
        <p className="text-[12px] text-dim mt-0.5">平台级 / 租户级 SSO 身份源与租户路由（→ identity）</p>
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

      {tab === 'platform' && <PlatformSsoTab />}
      {tab === 'tenant' && <TenantSsoTab />}
    </div>
  );
}
