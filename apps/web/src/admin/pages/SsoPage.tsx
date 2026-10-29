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
  DINGTALK_ENTERPRISE: 'DingTalk',
  FEISHU_ENTERPRISE: 'Feishu',
  WECOM: 'WeCom',
};

const KIND_OPTIONS = (Object.keys(KIND_LABELS) as SsoKind[]).map((k) => ({
  value: k,
  label: KIND_LABELS[k],
}));

const MATCH_TYPE_LABELS: Record<SsoMatchType, string> = {
  EMAIL_DOMAIN: 'Email domain',
  EQUALS: 'Equals',
  CONTAINS: 'Contains',
  REGEX: 'Regex',
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
    { key: 'certificate', label: 'Certificate', placeholder: '-----BEGIN CERTIFICATE-----', type: 'textarea' },
  ],
  OIDC: [
    { key: 'discovery_url', label: 'Discovery URL', placeholder: 'https://accounts.google.com/.well-known/openid-configuration' },
    { key: 'client_id', label: 'Client ID' },
    { key: 'client_secret', label: 'Client Secret', type: 'password' },
  ],
  AD: [
    { key: 'azure_tenant_id', label: 'Azure AD Tenant ID', placeholder: 'Azure AD tenant ID' },
    { key: 'client_id', label: 'Client ID' },
    { key: 'client_secret', label: 'Client Secret', type: 'password' },
  ],
  LDAP: [
    { key: 'server_url', label: 'Server URL', placeholder: 'ldap://ad.corp.com:389' },
    { key: 'base_dn', label: 'Base DN', placeholder: 'dc=corp,dc=com' },
    { key: 'bind_dn', label: 'Bind DN', placeholder: 'cn=admin,dc=corp,dc=com' },
    { key: 'bind_password', label: 'Bind password', type: 'password' },
    { key: 'user_search_filter', label: 'User search filter', placeholder: '(objectClass=person)' },
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
        {KIND_LABELS[kind]} settings
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
      toast.success('Identity provider created');
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="Add identity provider" onClose={onClose} dismissable={!busy}>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Type</label>
          <CustomSelect
            options={KIND_OPTIONS}
            value={kind}
            onChange={(v) => { setKind(v as SsoKind); setConfig({}); }}
            className="w-full"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Name</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Corporate domain account"
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
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? 'Creating…' : 'Create'}
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
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
      })
      .finally(() => setLoading(false));
  }, [selectedTenantId]);

  useEffect(() => { load(); }, [load]);

  const handleToggle = async (p: SsoProvider) => {
    if (p.enabled) {
      const yes = await confirm({
        title: 'Disable identity provider',
        message: `Disable "${p.name}"? Users who sign in through it will no longer be able to sign in.`,
        tone: 'danger',
        confirmText: 'Disable',
      });
      if (!yes) return;
      try {
        await api.admin.sso.disable(p.id, selectedTenantId ?? undefined);
        toast.success('Disabled');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : 'Operation failed');
      }
    } else {
      try {
        await api.admin.sso.enable(p.id, selectedTenantId ?? undefined);
        toast.success('Enabled');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : 'Operation failed');
      }
    }
  };

  const handleDelete = async (p: SsoProvider) => {
    const yes = await confirm({
      title: 'Delete identity provider',
      message: `Delete "${p.name}"? Its configuration will be permanently removed and linked users will need to re-link.`,
      tone: 'danger',
      confirmText: 'Delete',
    });
    if (!yes) return;
    try {
      await api.admin.sso.delete(p.id, selectedTenantId ?? undefined);
      toast.success('Deleted');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Failed to delete');
    }
  };

  return (
    <div className="flex flex-col gap-5">
      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">Tenant</label>
        <CustomSelect
          options={[
            { value: '', label: '(Default / current tenant)' },
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
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Identity providers</div>
        <button
          onClick={() => setCreating(true)}
          className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
        >
          + Add identity provider
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
      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Not available yet: the identity provider endpoint is unreachable.
        </div>
      )}
      {!loading && !todo && providers.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">No identity providers configured</div>
      )}

      {!loading && !todo && providers.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Name</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Type</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Created at</th>
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
                      <span className="text-green-700">Enabled</span>
                    ) : (
                      <span className="text-dim">Disabled</span>
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
                      {p.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button
                      onClick={() => handleDelete(p)}
                      className="text-[11px] font-mono text-accent hover:opacity-80"
                    >
                      Delete
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
      setError('A tenant-scoped identity provider requires a target tenant');
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
      toast.success('Identity provider created');
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="Add platform identity provider" onClose={onClose} dismissable={!busy}>
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Type</label>
          <CustomSelect
            options={KIND_OPTIONS}
            value={kind}
            onChange={(v) => { setKind(v as SsoKind); setConfig({}); }}
            className="w-full"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Name</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Corporate domain account"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Scope</label>
          <CustomSelect
            options={[
              { value: 'PLATFORM', label: 'Platform' },
              { value: 'TENANT', label: 'Tenant' },
            ]}
            value={scope}
            onChange={(v) => setScope(v as SsoScope)}
            className="w-full"
          />
        </div>
        {scope === 'TENANT' ? (
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">Target tenant</label>
            <CustomSelect
              options={[{ value: '', label: '(Select tenant)' }, ...tenantOptions]}
              value={tenantId}
              onChange={setTenantId}
              className="w-full"
            />
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">Default tenant</label>
            <CustomSelect
              options={[{ value: '', label: '(None)' }, ...tenantOptions]}
              value={defaultTenantId}
              onChange={setDefaultTenantId}
              className="w-full"
            />
            <span className="text-[10.5px] text-dim">Fallback tenant when no rule matches</span>
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
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? 'Creating…' : 'Create'}
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
  const [creating, setCreating] = useState(false);

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
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
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
      setError('Match value and target tenant are required');
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
      toast.success('Rule added');
      setPriority('100');
      setMatchType('EMAIL_DOMAIN');
      setMatchAttribute('email');
      setMatchValue('');
      setTargetTenantId('');
      setDefaultRole('');
      setCreating(false);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to add');
    } finally {
      setBusy(false);
    }
  };

  // Reset the add-rule form to defaults when the dialog is dismissed.
  const closeCreate = () => {
    setCreating(false);
    setError(null);
    setPriority('100');
    setMatchType('EMAIL_DOMAIN');
    setMatchAttribute('email');
    setMatchValue('');
    setTargetTenantId('');
    setDefaultRole('');
  };

  const handleToggleEnabled = async (r: SsoRoutingRule) => {
    try {
      await api.admin.sso.platform.rules.update(provider.id, r.id, { enabled: !r.enabled });
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Operation failed');
    }
  };

  const handleDeleteRule = async (r: SsoRoutingRule) => {
    const yes = await confirm({
      title: 'Delete routing rule',
      message: 'Delete this rule? It will stop taking effect.',
      tone: 'danger',
      confirmText: 'Delete',
    });
    if (!yes) return;
    try {
      await api.admin.sso.platform.rules.delete(provider.id, r.id);
      toast.success('Deleted');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Failed to delete');
    }
  };

  return (
    <div className="flex flex-col gap-3 bg-paper-2 border border-line-2 rounded-md p-4">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
          "{provider.name}" routing rules
        </div>
        {!todo && (
          <button
            onClick={() => setCreating(true)}
            className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
          >
            + Add rule
          </button>
        )}
      </div>

      {error && !creating && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}
      {todo && (
        <div className="border border-line rounded-md p-3 text-[12px] text-dim italic">
          Not available yet: the routing rules endpoint is unreachable.
        </div>
      )}

      {!loading && !todo && rules.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">No routing rules</div>
      )}

      {!loading && !todo && rules.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden bg-paper">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Priority</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Match attribute</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Match type</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Match value</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Target tenant</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Default role</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
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
                      {r.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => handleDeleteRule(r)}
                      className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-accent hover:opacity-80 transition-opacity"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Add rule dialog */}
      {creating && (
        <Dialog
          title="Add routing rule"
          onClose={closeCreate}
          dismissable={!busy}
          className="w-full max-w-[560px]"
        >
        <form onSubmit={handleAdd} className="flex flex-col gap-3">
          {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Priority</label>
              <input
                type="number"
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Match type</label>
              <CustomSelect
                options={MATCH_TYPE_OPTIONS}
                value={matchType}
                onChange={onMatchTypeChange}
                className="w-full"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Match attribute</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono disabled:opacity-50"
                value={matchAttribute}
                onChange={(e) => setMatchAttribute(e.target.value)}
                placeholder="department / groups / email"
                disabled={matchType === 'EMAIL_DOMAIN'}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Match value</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={matchValue}
                onChange={(e) => setMatchValue(e.target.value)}
                placeholder={valuePlaceholder}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Target tenant</label>
              <CustomSelect
                options={[{ value: '', label: '(Select tenant)' }, ...tenantOptions]}
                value={targetTenantId}
                onChange={setTargetTenantId}
                className="w-full"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Default role (optional)</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={defaultRole}
                onChange={(e) => setDefaultRole(e.target.value)}
                placeholder="member"
              />
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={closeCreate}
              disabled={busy}
              className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
            >
              {busy ? 'Adding…' : 'Add rule'}
            </button>
          </div>
        </form>
        </Dialog>
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
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleToggle = async (p: SsoProvider) => {
    if (p.enabled) {
      const yes = await confirm({
        title: 'Disable identity provider',
        message: `Disable "${p.name}"? Users who sign in through it will no longer be able to sign in.`,
        tone: 'danger',
        confirmText: 'Disable',
      });
      if (!yes) return;
      try {
        await api.admin.sso.platform.disable(p.id);
        toast.success('Disabled');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : 'Operation failed');
      }
    } else {
      try {
        await api.admin.sso.platform.enable(p.id);
        toast.success('Enabled');
        load();
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : 'Operation failed');
      }
    }
  };

  const handleDelete = async (p: SsoProvider) => {
    const yes = await confirm({
      title: 'Delete identity provider',
      message: `Delete "${p.name}"? Its configuration and routing rules will be permanently removed.`,
      tone: 'danger',
      confirmText: 'Delete',
    });
    if (!yes) return;
    try {
      await api.admin.sso.platform.delete(p.id);
      toast.success('Deleted');
      if (expandedId === p.id) setExpandedId(null);
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Failed to delete');
    }
  };

  const scopeLabel = (p: SsoProvider) =>
    p.scope === 'TENANT' ? `Tenant: ${tenantNameOf(p.tenant_id)}` : 'Platform';

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Platform identity providers</div>
        <button
          onClick={() => setCreating(true)}
          className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
        >
          + Add identity provider
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
      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Not available yet: the platform identity provider endpoint is unreachable.
        </div>
      )}
      {!loading && !todo && providers.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">No platform identity providers configured</div>
      )}

      {!loading && !todo && providers.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Name</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Type</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Scope</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Default tenant</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
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
                        <span className="text-green-700">Enabled</span>
                      ) : (
                        <span className="text-dim">Disabled</span>
                      )}
                    </td>
                    <td className="px-3 py-2 flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        onClick={() => handleToggle(p)}
                        className="text-[11px] font-mono text-ink-2 hover:text-ink"
                      >
                        {p.enabled ? 'Disable' : 'Enable'}
                      </button>
                      <button
                        onClick={() => setExpandedId((id) => (id === p.id ? null : p.id))}
                        className="text-[11px] font-mono text-ink-2 hover:text-ink"
                      >
                        Routing rules
                      </button>
                      <button
                        onClick={() => handleDelete(p)}
                        className="text-[11px] font-mono text-accent hover:opacity-80"
                      >
                        Delete
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
    { id: 'platform', label: 'Platform providers' },
    { id: 'tenant', label: 'Tenant providers' },
  ];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Identity providers</h1>
        <p className="text-[12px] text-dim mt-0.5">Platform and tenant SSO identity providers and tenant routing (→ identity)</p>
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
