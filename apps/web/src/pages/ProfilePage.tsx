import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { formatDateTimeAuto } from '../i18n/dates';
import { Chip } from '../components/shell/Chip';
import { useChatStore } from '../stores/chatStore';
import { useWorkspace } from '../lib/workspaceContext';
import { useTranslation } from '../i18n';
import { api } from '../api/client';
import { formatRelativeTime } from '../lib/format';
import type { ActiveSession, ApiKey, ApiKeyCreated, DailyTokenBreakdown, FeatureFlag, MfaMethod, ModelCatalogProvider, ModelPurpose, NotifPreferenceItem, QuotaInfo, TokenUsage, TokenUsageBreakdown, UserModelPreference, UserProfile } from '../api/types';
import { useDefaultModel, clearModelCache } from '../hooks/useDefaultModel';
import { MODEL_OPTIONS, SYSTEM_FALLBACK_MODEL, modelLabel } from '../lib/modelOptions';
import { findProviderForModel, getModelsForTypes } from '../lib/providerModels';

const FLAG_DESCRIPTIONS: Record<string, string> = {
  'ai.thinking_ui': 'AI thinking process',
  'billing.saas_mode': 'SaaS billing mode',
  'auth.social_login.google': 'Google social login',
  'auth.social_login.github': 'GitHub social login',
  'auth.social_login.wechat': 'WeChat social login',
  'export.allow_l4_download': 'L4 data download',
};

// ── Local sub-components ──────────────────────────────────────────

interface PfSectionProps {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}
function PfSection({ title, children, action }: PfSectionProps) {
  return (
    <div style={{
      border: '1px solid var(--line)',
      borderRadius: 'var(--r-md)',
      background: 'var(--surface)',
      overflow: 'hidden',
    }}>
      <div style={{
        padding: '9px 16px',
        borderBottom: '1px solid var(--line-2)',
        background: 'var(--paper)',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <span className="cw-label">{title}</span>
        <div style={{ flex: 1 }} />
        {action}
      </div>
      <div style={{ padding: '14px 16px' }}>{children}</div>
    </div>
  );
}

interface PfRowProps {
  label: string;
  sub?: string;
  children?: React.ReactNode;
}
function PfRow({ label, sub, children }: PfRowProps) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 500, letterSpacing: '-0.01em', marginBottom: sub ? 2 : 0 }}>
          {label}
        </div>
        {sub && (
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }}>{sub}</div>
        )}
      </div>
      {children}
    </div>
  );
}

interface PfFieldProps {
  label: string;
  value: string;
  onChange?: (v: string) => void;
  readonly?: boolean;
  inputRef?: React.Ref<HTMLInputElement>;
  onEnter?: () => void;
}
function PfField({ label, value, onChange, readonly, inputRef, onEnter }: PfFieldProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <label className="cw-label">{label}</label>
      <input
        ref={inputRef}
        value={value}
        onChange={onChange ? (e) => onChange(e.target.value) : undefined}
        onKeyDown={onEnter ? (e) => { if (e.key === 'Enter') onEnter(); } : undefined}
        readOnly={readonly}
        style={{
          padding: '7px 10px',
          border: '1px solid var(--line)',
          borderRadius: 'var(--r-sm)',
          fontFamily: 'var(--font-sans)',
          fontSize: 13.5,
          width: '100%',
          background: readonly ? 'var(--paper)' : 'var(--surface)',
          outline: 'none',
          color: readonly ? 'var(--ink-2)' : 'var(--ink)',
          cursor: readonly ? 'default' : 'text',
        }}
      />
    </div>
  );
}

interface PfSelectFieldProps {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}
function PfSelectField({ label, value, options, onChange }: PfSelectFieldProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <label className="cw-label">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          padding: '7px 10px',
          border: '1px solid var(--line)',
          borderRadius: 'var(--r-sm)',
          fontFamily: 'var(--font-sans)',
          fontSize: 13.5,
          width: '100%',
          background: 'var(--surface)',
          outline: 'none',
          color: 'var(--ink)',
          cursor: 'pointer',
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </div>
  );
}

interface PfToggleProps {
  on: boolean;
  onChange?: () => void;
}
function PfToggle({ on, onChange }: PfToggleProps) {
  return (
    <div
      onClick={onChange}
      style={{
        width: 30,
        height: 17,
        borderRadius: 9,
        background: on ? 'var(--accent)' : 'var(--line)',
        position: 'relative',
        cursor: onChange ? 'pointer' : 'default',
        transition: 'background 150ms ease',
        flexShrink: 0,
      }}
    >
      <div style={{
        position: 'absolute',
        top: 2,
        left: on ? 13 : 2,
        width: 13,
        height: 13,
        borderRadius: '50%',
        background: 'white',
        boxShadow: '0 1px 3px rgba(0,0,0,0.22)',
        transition: 'left 150ms ease',
      }} />
    </div>
  );
}

// Map a backend run-source code to its Chinese label; unknown codes pass through.
const SOURCE_LABELS: Record<string, string> = {
  user: 'Manual',
  schedule: 'Scheduled',
  app: 'App',
};
function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

interface StackedBarsProps {
  days: DailyTokenBreakdown[];
}
// Per-day stacked bar chart: output_tokens (bottom, --accent) + input_tokens
// (top, muted) so the full bar height ∝ total_tokens. Mirrors the old SparkBars
// proportions (BAR_H 60, MM-DD labels, today highlighted, all-zero hint).
function StackedBars({ days }: StackedBarsProps) {
  const m = Math.max(...days.map((d) => d.total_tokens), 1);  // avoid division by zero
  const BAR_H = 60;
  const allZero = days.every((d) => d.total_tokens === 0);
  return (
    <div style={{ display: 'flex', gap: 7, height: BAR_H + 22, alignItems: 'flex-end' }}>
      {days.map((d, i) => {
        const isToday = i === days.length - 1;
        const totalPx = allZero ? 3 : Math.max((d.total_tokens / m) * BAR_H, d.total_tokens > 0 ? 3 : 0);
        // Split the total height between output (bottom) and input (top).
        const outPx = d.total_tokens > 0 ? (d.output_tokens / d.total_tokens) * totalPx : 0;
        const inPx = Math.max(totalPx - outPx, 0);
        return (
          <div
            key={d.day}
            title={`${d.day}\nInput ${d.input_tokens.toLocaleString()} · Output ${d.output_tokens.toLocaleString()} · Total ${d.total_tokens.toLocaleString()} tokens`}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5 }}
          >
            <div style={{
              width: '100%',
              height: `${totalPx}px`,
              display: 'flex',
              flexDirection: 'column',
              justifyContent: 'flex-end',
              borderRadius: '3px 3px 0 0',
              overflow: 'hidden',
            }}>
              {/* input — top segment, muted */}
              <div style={{
                width: '100%',
                height: `${inPx}px`,
                background: allZero ? 'var(--line-2)' : 'var(--line)',
                transition: 'height 120ms',
              }} />
              {/* output — bottom segment, accent (dimmed on today=false to echo SparkBars highlight) */}
              <div style={{
                width: '100%',
                height: `${outPx}px`,
                background: allZero ? 'var(--line-2)' : isToday ? 'var(--accent)' : 'var(--accent)',
                opacity: allZero ? 1 : isToday ? 1 : 0.78,
                transition: 'height 120ms, opacity 120ms',
              }} />
            </div>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--dim)' }}>
              {d.day.slice(5)}  {/* show MM-DD */}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// Small swatch + label legend used under the stacked daily chart.
function ChartLegend() {
  const item = (color: string, opacity: number, label: string) => (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
      <span style={{ width: 9, height: 9, borderRadius: 2, background: color, opacity, display: 'inline-block' }} />
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--dim)' }}>{label}</span>
    </span>
  );
  return (
    <div style={{ display: 'flex', gap: 14, marginTop: 10 }}>
      {item('var(--accent)', 0.78, 'Output')}
      {item('var(--line)', 1, 'Input')}
    </div>
  );
}


// ── Main page ─────────────────────────────────────────────────────

type TabId = 'account' | 'usage' | 'security' | 'notifications';

const TABS: { id: TabId; label: string }[] = [
  { id: 'account',       label: 'Account' },
  { id: 'usage',         label: 'Usage & billing' },
  { id: 'security',      label: 'Security' },
  { id: 'notifications', label: 'Notifications' },
];

export function ProfilePage() {
  const navigate = useNavigate();
  const userProfile    = useChatStore((s) => s.userProfile);
  const logout         = useChatStore((s) => s.logout);
  const setUserProfile = useChatStore((s) => s.setUserProfile);
  const { allWs }      = useWorkspace();
  const { lang, setLang } = useTranslation();

  // Real profile data fetched from api.auth.me() — supplements the store cache.
  const [liveProfile, setLiveProfile] = useState<UserProfile | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.auth.me().then((p) => {
      if (!cancelled) setLiveProfile(p);
    }).catch((_e: unknown) => {
      // best-effort — fall back to store data
    });
    return () => { cancelled = true; };
  }, []);

  // Real billing data — plan + quota usage for the current tenant.
  const [billingInfo, setBillingInfo] = useState<QuotaInfo | null>(null);
  const [billingError, setBillingError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.billing.me().then((info) => {
      if (!cancelled) setBillingInfo(info);
    }).catch((_e: unknown) => {
      if (!cancelled) setBillingError(true);
    });
    return () => { cancelled = true; };
  }, []);

  // Real MFA methods — non-empty list means 2FA is enabled.
  const [mfaMethods, setMfaMethods] = useState<MfaMethod[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.auth.mfaMethods().then((methods) => {
      if (!cancelled) setMfaMethods(methods);
    }).catch((_e: unknown) => {
      // best-effort — treat as unknown (don't crash)
      if (!cancelled) setMfaMethods([]);
    });
    return () => { cancelled = true; };
  }, []);

  // Prefer live API data, then store cache, then placeholder.
  const profile     = liveProfile ?? userProfile;
  const displayName = profile?.display_name ?? 'You';
  const email       = profile?.email ?? 'you@acme.co';
  const initial     = displayName[0]?.toUpperCase() ?? 'Y';

  // Real run count for the "运行任务数" stat tile.
  const [runsCount, setRunsCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.runs.count().then((data) => {
      if (!cancelled) setRunsCount(data.count);
    }).catch(() => { /* best-effort */ });
    return () => { cancelled = true; };
  }, []);

  // Real token usage totals across all runs for the "本月 Token" stat tile.
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.runs.tokenUsage().then((data) => {
      if (!cancelled) setTokenUsage(data);
    }).catch(() => { /* best-effort */ });
    return () => { cancelled = true; };
  }, []);

  // Multi-dimensional token-usage breakdown (daily input/output split + by
  // source) for the usage dashboard. Refetched whenever the time window changes.
  const [usageDays, setUsageDays] = useState(30);
  const [breakdown, setBreakdown] = useState<TokenUsageBreakdown | null>(null);
  const [breakdownError, setBreakdownError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBreakdown(null);
    setBreakdownError(false);
    api.runs.tokenUsageBreakdown(usageDays).then((data) => {
      if (!cancelled) setBreakdown(data);
    }).catch(() => {
      if (!cancelled) setBreakdownError(true);
    });
    return () => { cancelled = true; };
  }, [usageDays]);

  // Real active login sessions from identity.
  const [authSessions, setAuthSessions] = useState<ActiveSession[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.auth.sessions().then((data) => {
      if (!cancelled) setAuthSessions(data);
    }).catch(() => {
      if (!cancelled) setAuthSessions([]);
    });
    return () => { cancelled = true; };
  }, []);

  // Real API keys — list, create, revoke
  const [apiKeys, setApiKeys]         = useState<ApiKey[]>([]);
  const [apiKeysLoading, setApiKeysLoading] = useState(false);
  const [newKeyName, setNewKeyName]   = useState('');
  const [createdKey, setCreatedKey]   = useState<ApiKeyCreated | null>(null);
  const [copyDone, setCopyDone]       = useState(false);
  const [apiKeysError, setApiKeysError] = useState<string | null>(null);

  const fetchApiKeys = () => {
    setApiKeysLoading(true);
    setApiKeysError(null);
    api.apiKeys.list().then((keys) => {
      setApiKeys(keys);
    }).catch(() => {
      setApiKeysError('Failed to load API keys');
    }).finally(() => {
      setApiKeysLoading(false);
    });
  };

  useEffect(() => {
    fetchApiKeys();
  }, []);

  const handleCreateKey = async () => {
    const name = newKeyName.trim();
    if (!name) return;
    try {
      const created = await api.apiKeys.create(name);
      setCreatedKey(created);
      setNewKeyName('');
      fetchApiKeys();
    } catch {
      setApiKeysError('Failed to create API key');
    }
  };

  const handleRevokeKey = async (id: string) => {
    try {
      await api.apiKeys.revoke(id);
      setApiKeys((prev) => prev.filter((k) => k.id !== id));
    } catch {
      setApiKeysError('Failed to revoke API key');
    }
  };

  const handleCopyKey = () => {
    if (!createdKey) return;
    navigator.clipboard.writeText(createdKey.key).catch(() => {});
    setCopyDone(true);
    setTimeout(() => setCopyDone(false), 2000);
  };

  const [tab,      setTab]      = useState<TabId>('account');
  const [name,     setName]     = useState(displayName);
  const [digestOn, setDigest]   = useState(true);

  // ── Profile edit mode (个人信息) ─────────────────────────────────
  const [editingProfile, setEditingProfile] = useState(false);
  const [savingProfile,  setSavingProfile]  = useState(false);
  const [profileMsg,     setProfileMsg]     = useState<string | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);

  // Keep the editable draft in sync with the live profile while NOT editing,
  // so the read-only field always shows the REAL current name (the mount-time
  // useState(displayName) ran before liveProfile resolved).
  useEffect(() => {
    if (!editingProfile) setName(displayName);
  }, [displayName, editingProfile]);

  // Focus the 显示名称 input when entering edit mode.
  useEffect(() => {
    if (editingProfile) nameInputRef.current?.focus();
  }, [editingProfile]);

  const enterProfileEdit = () => {
    setName(displayName);
    setEditingProfile(true);
  };

  const cancelProfileEdit = () => {
    setName(displayName);
    setEditingProfile(false);
    setProfileMsg(null);
  };

  const handleSaveProfile = async () => {
    const trimmed = name.trim();
    // No-op when empty or unchanged — just leave edit mode.
    if (!trimmed || trimmed === displayName) {
      cancelProfileEdit();
      return;
    }
    setSavingProfile(true);
    setProfileMsg(null);
    try {
      const updated = await api.auth.updateProfile({ display_name: trimmed });
      setLiveProfile(updated);
      setUserProfile(updated); // refresh sidebar / account-menu avatar immediately
      setEditingProfile(false);
      setProfileMsg('Saved');
      setTimeout(() => setProfileMsg(null), 2000);
    } catch {
      // Stay in edit mode so the user can retry.
      setProfileMsg('Save failed');
      setTimeout(() => setProfileMsg(null), 2000);
    } finally {
      setSavingProfile(false);
    }
  };

  // ── Default model preference (real data) ────────────────────────
  const purpose: ModelPurpose = 'llm';
  const { modelId: resolvedModelId, source: modelSource, refetch: refetchDefault } = useDefaultModel(purpose);

  // User's explicit preference (if any) from the API
  const [userPref, setUserPref] = useState<UserModelPreference | null>(null);
  const [userPrefLoading, setUserPrefLoading] = useState(true);
  const [userPrefError, setUserPrefError] = useState<string | null>(null);

  const fetchUserPref = () => {
    setUserPrefLoading(true);
    setUserPrefError(null);
    api.modelPreferences.list().then((prefs) => {
      const match = prefs.find((p) => p.purpose === purpose);
      setUserPref(match ?? null);
    }).catch(() => {
      setUserPrefError('Failed to load model preference');
    }).finally(() => {
      setUserPrefLoading(false);
    });
  };

  useEffect(() => { fetchUserPref(); }, [purpose]);

  // Available providers for the model dropdown
  const [providers, setProviders] = useState<ModelCatalogProvider[]>([]);
  const [providersError, setProvidersError] = useState<string | null>(null);

  useEffect(() => {
    // Non-admin, sanitized catalogue endpoint (no credentials). Only enabled
    // providers are returned by the BFF, so no client-side filter is needed.
    api.modelPreferences.listModels().then((items) => {
      setProviders(items);
      setProvidersError(null);
    }).catch((err) => {
      // Don't fail silently — the dropdown falls back to MODEL_OPTIONS, but a
      // failure here also breaks findProviderForModel, so surface it.
      console.warn('Failed to load model list', err);
      setProvidersError('Failed to load model list; using the built-in list');
    });
  }, []);

  // Build option list: enumerate each provider's LLM models (covering both the
  // model_type_map['llm'] entries and the flat `models` list), falling back to
  // the static MODEL_OPTIONS when no provider advertises any model.
  const modelOptions = (() => {
    const opts = providers.flatMap((p) => {
      const ids = new Set([...getModelsForTypes(p, ['llm']), ...(p.models ?? [])]);
      return Array.from(ids).map((m) => ({ id: m, label: `${m} (${p.name})` }));
    });
    return opts.length > 0 ? opts : MODEL_OPTIONS;
  })();

  // Currently selected model in the dropdown (editable, not yet saved)
  const [draftModel, setDraftModel] = useState<string | null>(null);
  const [savingModel, setSavingModel] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  // Once the user picks a model manually, stop overwriting the draft from
  // async loads (userPref / resolve race in whichever order they land).
  const draftTouched = useRef(false);

  // Sync draft with the loaded user preference or resolved default
  useEffect(() => {
    if (!userPrefLoading && !draftTouched.current) {
      setDraftModel(userPref?.model_id ?? resolvedModelId ?? SYSTEM_FALLBACK_MODEL);
    }
  }, [userPrefLoading, userPref, resolvedModelId]);

  // Auto-save the picked model immediately on selection (no explicit save button).
  const handleSelectModel = async (modelId: string) => {
    draftTouched.current = true;
    setDraftModel(modelId);
    // Resolve the owning provider via type_map + flat models. A model with no
    // resolvable provider must not be saved with an empty provider_id.
    const providerId = findProviderForModel(providers, modelId, ['llm']);
    if (!providerId) {
      setSaveMsg('Couldn’t determine the provider for this model');
      setTimeout(() => setSaveMsg(null), 3000);
      return;
    }
    setSavingModel(true);
    setSaveMsg(null);
    try {
      await api.modelPreferences.upsert({
        purpose,
        model_id: modelId,
        provider_id: providerId,
      });
      clearModelCache(purpose);
      draftTouched.current = false; // re-sync draft from the refreshed data
      refetchDefault();
      fetchUserPref();
      setSaveMsg('Saved');
      setTimeout(() => setSaveMsg(null), 2000);
    } catch {
      setSaveMsg('Save failed');
      setTimeout(() => setSaveMsg(null), 3000);
    } finally {
      setSavingModel(false);
    }
  };

  // Real notification preferences
  const [notifPrefs, setNotifPrefs] = useState<NotifPreferenceItem[]>([]);
  const [notifPrefsErr, setNotifPrefsErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.notifications.preferences().then((data) => {
      if (!cancelled) setNotifPrefs(data.items);
    }).catch((_e: unknown) => {
      if (!cancelled) setNotifPrefsErr('Failed to load notification preferences');
    });
    return () => { cancelled = true; };
  }, []);

  const toggleNotif = (eventKey: string, field: 'email_enabled' | 'push_enabled') => {
    setNotifPrefs((prev) => {
      const updated = prev.map((n) =>
        n.event_key === eventKey ? { ...n, [field]: !n[field] } : n,
      );
      const changed = updated.find((n) => n.event_key === eventKey);
      if (changed) {
        api.notifications.updatePreferences([{
          event_key: changed.event_key,
          email_enabled: changed.email_enabled,
          push_enabled: changed.push_enabled,
        }]).catch((_e: unknown) => {
          setNotifPrefsErr('Failed to save notification preferences');
        });
      }
      return updated;
    });
  };

  // ── Feature flag preferences (user-overridable flags) ───────────
  const [featureFlags, setFeatureFlags] = useState<FeatureFlag[]>([]);
  const [ffSaving, setFfSaving] = useState<string | null>(null);
  const [ffError, setFfError] = useState<string | null>(null);

  const fetchFlags = () => {
    api.featureFlags.list()
      .then(setFeatureFlags)
      .catch(() => setFfError('Failed to load feature settings'));
  };
  useEffect(() => { fetchFlags(); }, []);

  const overridableFlags = featureFlags.filter((f) => f.user_overridable);

  const handleToggleFlag = async (key: string, currentEnabled: boolean) => {
    setFfSaving(key);
    setFfError(null);
    try {
      await api.userFlagOverrides.put(key, { enabled: !currentEnabled });
      fetchFlags();
    } catch {
      setFfError('Save failed');
      setTimeout(() => setFfError(null), 3000);
    } finally {
      setFfSaving(null);
    }
  };

  const handleResetFlag = async (key: string) => {
    setFfSaving(key);
    setFfError(null);
    try {
      await api.userFlagOverrides.delete(key);
      fetchFlags();
    } catch {
      setFfError('Reset failed');
      setTimeout(() => setFfError(null), 3000);
    } finally {
      setFfSaving(null);
    }
  };

  const handleLogout = async () => {
    try {
      await api.auth.logout();
    } catch {
      // best-effort
    }
    logout();
    navigate('/home');
  };

  return (
    <div className="flex flex-col h-full" data-screen-label="Profile">
      <PageHead crumbs={[{ label: 'Home', to: '/home' }, { label: 'Profile' }]} />

      <div className="flex-1 overflow-y-auto" style={{ padding: '28px 32px 52px' }}>
        <div style={{ maxWidth: 860, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 18 }}>

          {/* ── Profile header ─────────────────────────────────── */}
          <div style={{
            border: '1px solid var(--line)',
            borderRadius: 'var(--r-lg)',
            background: 'var(--surface)',
            padding: '22px 22px 18px',
            display: 'flex',
            alignItems: 'flex-start',
            gap: 20,
            position: 'relative',
            overflow: 'hidden',
          }}>
            {/* accent left-bar */}
            <div style={{
              position: 'absolute', left: 0, top: 0, bottom: 0, width: 3, background: 'var(--accent)',
            }} />

            {/* avatar */}
            <div style={{
              width: 62,
              height: 62,
              borderRadius: '50%',
              background: 'var(--accent-soft)',
              border: '1.5px solid var(--accent-line)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontFamily: 'var(--font-display)',
              fontStyle: 'italic',
              fontSize: 28,
              color: 'var(--accent)',
              flexShrink: 0,
              marginLeft: 8,
            }}>
              {initial}
            </div>

            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 3 }}>
                <span style={{
                  fontFamily: 'var(--font-display)',
                  fontSize: 22,
                  letterSpacing: '-0.01em',
                  lineHeight: 1.15,
                  color: 'var(--ink)',
                }}>
                  {displayName}
                </span>
                {billingInfo?.plan && <Chip accent>{billingInfo.plan.tier}</Chip>}
              </div>
              <div style={{
                fontSize: 13,
                color: 'var(--ink-2)',
                marginBottom: 14,
                fontFamily: 'var(--font-mono)',
              }}>
                {email}
              </div>

              {/* stat tiles — run count and cumulative tokens from real API */}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {[
                  { l: 'Runs', v: runsCount !== null ? String(runsCount) : '…' },
                  {
                    l: 'Total tokens',
                    v: tokenUsage !== null
                      ? tokenUsage.total_tokens.toLocaleString()
                      : '…',
                  },
                ].map((s) => (
                  <div key={s.l} style={{
                    padding: '5px 12px',
                    border: '1px solid var(--line)',
                    borderRadius: 'var(--r-sm)',
                    background: 'var(--paper)',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 2,
                  }}>
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--dim)' }}>
                      {s.l}
                    </span>
                    <span style={{
                      fontWeight: 700,
                      fontSize: 14.5,
                      fontFamily: 'var(--font-mono)',
                      letterSpacing: '-0.03em',
                    }}>
                      {s.v}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            <button
              className="cw-btn cw-btn-sm cw-btn-ghost"
              onClick={() => { setTab('account'); enterProfileEdit(); }}
            >
              Edit profile
            </button>
          </div>

          {/* ── Tabs ───────────────────────────────────────────── */}
          <div style={{ display: 'flex', borderBottom: '1px solid var(--line)' }}>
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                style={{
                  padding: '7px 15px',
                  border: 'none',
                  borderBottom: `2px solid ${tab === t.id ? 'var(--accent)' : 'transparent'}`,
                  background: 'transparent',
                  fontFamily: 'var(--font-sans)',
                  fontSize: 13,
                  fontWeight: tab === t.id ? 600 : 400,
                  color: tab === t.id ? 'var(--ink)' : 'var(--ink-2)',
                  cursor: 'pointer',
                  letterSpacing: '-0.005em',
                  marginBottom: -1,
                  transition: 'color 120ms, border-color 120ms',
                  whiteSpace: 'nowrap',
                }}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* ══ 账户 ════════════════════════════════════════════ */}
          {tab === 'account' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

              <PfSection
                title="Profile"
                action={
                  editingProfile ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {profileMsg && (
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: profileMsg.includes('failed') ? 'var(--accent)' : 'var(--ink-2)' }}>
                          {profileMsg}
                        </span>
                      )}
                      <button
                        className="cw-btn cw-btn-sm cw-btn-ghost"
                        onClick={cancelProfileEdit}
                        disabled={savingProfile}
                      >
                        Cancel
                      </button>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-primary"
                        onClick={handleSaveProfile}
                        disabled={savingProfile}
                      >
                        {savingProfile ? 'Saving…' : 'Save changes'}
                      </button>
                    </div>
                  ) : (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {profileMsg && (
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: profileMsg.includes('failed') ? 'var(--accent)' : 'var(--ink-2)' }}>
                          {profileMsg}
                        </span>
                      )}
                      <button className="cw-btn cw-btn-sm" onClick={enterProfileEdit}>
                        Edit
                      </button>
                    </div>
                  )
                }
              >
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                  <PfField
                    label="Display name"
                    value={name}
                    onChange={editingProfile ? setName : undefined}
                    readonly={!editingProfile}
                    inputRef={nameInputRef}
                    onEnter={editingProfile ? handleSaveProfile : undefined}
                  />
                  <PfField label="Email address" value={email}              readonly />
                  <PfField label="Time zone"   value={Intl.DateTimeFormat().resolvedOptions().timeZone} readonly />
                  <PfSelectField
                    label="Language"
                    value={lang}
                    onChange={(v) => setLang(v as 'zh' | 'en')}
                    options={[
                      { value: 'zh', label: '简体中文' },
                      { value: 'en', label: 'English' },
                    ]}
                  />
                </div>
              </PfSection>

              <PfSection title="Worker defaults">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="Default model" sub={
                    userPrefLoading ? 'Loading…'
                      : userPref ? `Source: your preference (${modelLabel(userPref.model_id)})`
                      : modelSource === 'tenant' ? 'Source: tenant default'
                      : modelSource === 'system' ? 'Source: system default'
                      : 'Model used when no template specifies one'
                  }>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <select
                        value={draftModel ?? ''}
                        onChange={(e) => handleSelectModel(e.target.value)}
                        disabled={savingModel}
                        style={{
                          padding: '5px 10px',
                          border: '1px solid var(--line)',
                          borderRadius: 'var(--r-sm)',
                          fontFamily: 'var(--font-mono)',
                          fontSize: 11,
                          background: 'var(--surface)',
                          color: 'var(--ink)',
                          outline: 'none',
                          cursor: savingModel ? 'default' : 'pointer',
                          minWidth: 180,
                        }}
                      >
                        {modelOptions.map((o) => (
                          <option key={o.id} value={o.id}>{o.label}</option>
                        ))}
                      </select>
                      {(savingModel || saveMsg) && (
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: saveMsg?.includes('failed') ? 'var(--accent)' : 'var(--ink-2)' }}>
                          {savingModel ? 'Saving…' : saveMsg}
                        </span>
                      )}
                    </div>
                  </PfRow>
                  {userPrefError && (
                    <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--accent)' }}>
                      {userPrefError}
                    </div>
                  )}
                  {providersError && (
                    <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--accent)' }}>
                      {providersError}
                    </div>
                  )}
                </div>
              </PfSection>

              {overridableFlags.length > 0 && (
                <PfSection title="Features">
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                    {ffError && (
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--accent)' }}>
                        {ffError}
                      </div>
                    )}
                    {overridableFlags.map((flag, i) => (
                      <div key={flag.key}>
                        {i > 0 && <div className="cw-divider" style={{ marginBottom: 13 }} />}
                        <PfRow
                          label={FLAG_DESCRIPTIONS[flag.key] || flag.name || flag.key}
                          sub={flag.key}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <PfToggle
                              on={!!flag.enabled && !flag.kill_switch_active}
                              onChange={() => handleToggleFlag(flag.key, !!flag.enabled && !flag.kill_switch_active)}
                            />
                            {flag.user_override != null && (
                              <button
                                className="cw-btn cw-btn-sm cw-btn-ghost"
                                style={{ fontSize: 11 }}
                                onClick={() => handleResetFlag(flag.key)}
                                disabled={ffSaving === flag.key}
                              >
                                Reset to default
                              </button>
                            )}
                            {ffSaving === flag.key && (
                              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--dim)' }}>
                                Saving…
                              </span>
                            )}
                          </div>
                        </PfRow>
                      </div>
                    ))}
                  </div>
                </PfSection>
              )}

              <PfSection title="Workspaces">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {allWs.map((w) => (
                    <div key={w.id} style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 12,
                      padding: '9px 12px',
                      border: '1px solid var(--line-2)',
                      borderRadius: 'var(--r-sm)',
                      background: 'var(--paper)',
                    }}>
                      <span style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        width: 24,
                        height: 24,
                        borderRadius: 'var(--r-sm)',
                        fontFamily: 'var(--font-mono)',
                        fontSize: 12,
                        fontWeight: 600,
                        flexShrink: 0,
                        background: w.accent + '14',
                        border: `1px solid ${w.accent}55`,
                        color: w.accent,
                      }}>
                        {w.glyph}
                      </span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, letterSpacing: '-0.01em' }}>{w.name}</div>
                        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--dim)' }}>{w.desc}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </PfSection>

            </div>
          )}

          {/* ══ 用量与账单 ══════════════════════════════════════ */}
          {tab === 'usage' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

              {/* ── Time-range segmented control ───────────────── */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span className="cw-label">Time range</span>
                <div style={{ flex: 1 }} />
                <div style={{
                  display: 'inline-flex',
                  border: '1px solid var(--line)',
                  borderRadius: 'var(--r-sm)',
                  overflow: 'hidden',
                }}>
                  {[
                    { d: 7,  label: 'Last 7 days' },
                    { d: 30, label: 'Last 30 days' },
                    { d: 90, label: 'Last 90 days' },
                  ].map((opt, i) => {
                    const active = usageDays === opt.d;
                    return (
                      <button
                        key={opt.d}
                        onClick={() => setUsageDays(opt.d)}
                        style={{
                          padding: '4px 12px',
                          border: 'none',
                          borderLeft: i === 0 ? 'none' : '1px solid var(--line)',
                          background: active ? 'var(--accent)' : 'var(--surface)',
                          color: active ? 'white' : 'var(--ink-2)',
                          fontFamily: 'var(--font-mono)',
                          fontSize: 11,
                          fontWeight: active ? 600 : 400,
                          cursor: 'pointer',
                          transition: 'background 120ms, color 120ms',
                        }}
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* ── 每日 Token 用量（输入/输出堆叠）— GET /runs/token-usage/breakdown ── */}
              {breakdownError ? (
                <PfSection title={`Token usage (last ${usageDays} days)`}>
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>Failed to load usage data</span>
                </PfSection>
              ) : breakdown === null ? (
                <PfSection title={`Token usage (last ${usageDays} days)`}>
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>Loading…</span>
                </PfSection>
              ) : (() => {
                const total = breakdown.daily.reduce((a, d) => a + d.total_tokens, 0);
                const allZero = breakdown.daily.every((d) => d.total_tokens === 0);
                return (
                  <PfSection
                    title={`Token usage (last ${usageDays} days) — ${total.toLocaleString()} tokens total`}
                  >
                    {allZero ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                        <StackedBars days={breakdown.daily} />
                        <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)', textAlign: 'center' }}>
                          No token usage in the last {usageDays} days
                        </span>
                      </div>
                    ) : (
                      <>
                        <StackedBars days={breakdown.daily} />
                        <ChartLegend />
                      </>
                    )}
                  </PfSection>
                );
              })()}

              {/* ── 按来源 — GET /runs/token-usage/breakdown by_source ── */}
              {breakdown !== null && !breakdownError && (() => {
                const sources = breakdown.by_source;
                const sourceTotal = sources.reduce((a, s) => a + s.total_tokens, 0);
                const maxSource = Math.max(...sources.map((s) => s.total_tokens), 1);
                return (
                  <PfSection title="By source">
                    {sources.length === 0 ? (
                      <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>No usage data yet</span>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        {sources.map((s) => {
                          const pct = sourceTotal > 0 ? (s.total_tokens / sourceTotal) * 100 : 0;
                          const barW = (s.total_tokens / maxSource) * 100;
                          return (
                            <div key={s.source} style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                                <span style={{ fontSize: 13, fontWeight: 500, letterSpacing: '-0.01em' }}>
                                  {sourceLabel(s.source)}
                                </span>
                                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                                  {s.run_count.toLocaleString()} {s.run_count === 1 ? 'run' : 'runs'}
                                </span>
                                <div style={{ flex: 1 }} />
                                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--ink-2)' }}>
                                  {s.total_tokens.toLocaleString()}
                                </span>
                                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)', minWidth: 42, textAlign: 'right' }}>
                                  {pct.toFixed(1)}%
                                </span>
                              </div>
                              <div style={{
                                width: '100%',
                                height: 8,
                                borderRadius: 4,
                                background: 'var(--line-2)',
                                overflow: 'hidden',
                              }}>
                                <div style={{
                                  width: `${barW}%`,
                                  height: '100%',
                                  background: 'var(--accent)',
                                  borderRadius: 4,
                                  transition: 'width 150ms',
                                }} />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </PfSection>
                );
              })()}

              {/* 套餐 — read-only display, real data from GET /billing/me */}
              <PfSection title="Plan">
                {billingError ? (
                  <span className="cw-label" style={{ fontStyle: 'italic' }}>Plan data is unavailable</span>
                ) : billingInfo === null ? (
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>Loading…</span>
                ) : billingInfo.plan === null ? (
                  <span className="cw-label" style={{ fontStyle: 'italic' }}>No active subscription</span>
                ) : (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                    <div style={{
                      border: '1.5px solid var(--accent-line)',
                      borderRadius: 'var(--r-md)',
                      padding: '14px 18px',
                      background: 'var(--accent-soft)',
                      flex: 1,
                    }}>
                      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }}>
                        <span style={{
                          fontFamily: 'var(--font-display)',
                          fontSize: 22,
                          color: 'var(--accent)',
                          fontStyle: 'italic',
                        }}>
                          {billingInfo.plan.name}
                        </span>
                        <Chip>{billingInfo.plan.tier}</Chip>
                      </div>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        {Object.entries(billingInfo.plan.limits).map(([k, v]) => (
                          <Chip key={k}>{k}: {v}</Chip>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </PfSection>

            </div>
          )}

          {/* ══ 安全 ════════════════════════════════════════════ */}
          {tab === 'security' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

              {/* API keys — DESIGN FIXTURE (no backend API key management endpoint) */}
              <PfSection title="API keys">
                {/* Newly created key — shown once, then discarded */}
                {createdKey && (
                  <div style={{
                    marginBottom: 12,
                    padding: '10px 12px',
                    border: '1px solid var(--accent)',
                    borderRadius: 'var(--r-sm)',
                    background: 'var(--accent-soft)',
                  }}>
                    <div className="cw-label" style={{ marginBottom: 6 }}>
                      New key (shown only once — copy and store it now)
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <code style={{
                        flex: 1,
                        fontFamily: 'var(--font-mono)',
                        fontSize: 12,
                        color: 'var(--ink)',
                        wordBreak: 'break-all',
                      }}>
                        {createdKey.key}
                      </code>
                      <button
                        className="cw-btn cw-btn-sm"
                        onClick={handleCopyKey}
                      >
                        {copyDone ? 'Copied' : 'Copy'}
                      </button>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-ghost"
                        onClick={() => setCreatedKey(null)}
                      >
                        Close
                      </button>
                    </div>
                  </div>
                )}

                {/* Create form */}
                <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
                  <input
                    value={newKeyName}
                    onChange={(e) => setNewKeyName(e.target.value)}
                    placeholder="New key name, e.g. Production"
                    onKeyDown={(e) => { if (e.key === 'Enter') handleCreateKey(); }}
                    style={{
                      flex: 1,
                      padding: '7px 10px',
                      border: '1px solid var(--line)',
                      borderRadius: 'var(--r-sm)',
                      fontFamily: 'var(--font-sans)',
                      fontSize: 13,
                      outline: 'none',
                    }}
                  />
                  <button
                    className="cw-btn cw-btn-sm cw-btn-primary"
                    onClick={handleCreateKey}
                    disabled={!newKeyName.trim()}
                  >
                    + New key
                  </button>
                </div>

                {apiKeysError && (
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--accent)', marginBottom: 8 }}>
                    {apiKeysError}
                  </div>
                )}

                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  <div style={{
                    display: 'grid',
                    gridTemplateColumns: '1.3fr 1fr 0.8fr 0.8fr auto',
                    gap: '6px 14px',
                    padding: '5px 4px 9px',
                    borderBottom: '1px solid var(--line-2)',
                  }}>
                    {['Name', 'Prefix', 'Created', 'Last used', ''].map((h) => (
                      <span key={h} className="cw-label">{h}</span>
                    ))}
                  </div>

                  {apiKeysLoading && (
                    <div style={{ padding: '12px 4px', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                      Loading…
                    </div>
                  )}

                  {!apiKeysLoading && apiKeys.length === 0 && (
                    <div style={{ padding: '12px 4px', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)', fontStyle: 'italic' }}>
                      No API keys yet
                    </div>
                  )}

                  {apiKeys.map((k) => (
                    <div key={k.id} style={{
                      display: 'grid',
                      gridTemplateColumns: '1.3fr 1fr 0.8fr 0.8fr auto',
                      gap: '6px 14px',
                      padding: '10px 4px',
                      borderBottom: '1px solid var(--line-2)',
                      alignItems: 'center',
                    }}>
                      <div style={{ fontSize: 13, fontWeight: 500 }}>{k.name}</div>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11.5, color: 'var(--ink-2)' }}>
                        {k.key_prefix}…
                      </span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                        {formatRelativeTime(k.created_at)}
                      </span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                        {k.last_used_at ? formatRelativeTime(k.last_used_at) : 'Never'}
                      </span>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-ghost"
                        style={{ fontSize: 11, color: 'var(--dim)', padding: '3px 8px' }}
                        onClick={() => handleRevokeKey(k.id)}
                      >
                        Revoke
                      </button>
                    </div>
                  ))}
                </div>
              </PfSection>

              {/* Active sessions — real data from identity GET /sessions */}
              {authSessions !== null && authSessions.length > 0 && (
                <PfSection title="Active sessions">
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {authSessions.map((s) => {
                      const isMobile = s.device.includes('iOS') || s.device.includes('Android');
                      const timeLabel = s.last_seen_at
                        ? formatDateTimeAuto(new Date(s.last_seen_at), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                        : formatDateTimeAuto(new Date(s.issued_at), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
                      return (
                        <div key={s.id} style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 12,
                          padding: '10px 12px',
                          border: `1px solid ${s.current ? 'var(--accent-line)' : 'var(--line-2)'}`,
                          borderRadius: 'var(--r-sm)',
                          background: s.current ? 'var(--accent-soft)' : 'var(--paper)',
                        }}>
                          <div style={{
                            width: 32,
                            height: 32,
                            border: '1px solid var(--line)',
                            borderRadius: 'var(--r-sm)',
                            background: 'var(--surface)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            fontFamily: 'var(--font-mono)',
                            fontSize: 14,
                            color: s.current ? 'var(--accent)' : 'var(--dim)',
                            flexShrink: 0,
                          }}>
                            {isMobile ? '▣' : '□'}
                          </div>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{
                              fontSize: 13,
                              fontWeight: 500,
                              display: 'flex',
                              alignItems: 'center',
                              gap: 8,
                            }}>
                              {s.device}
                              {s.current && (
                                <span style={{
                                  fontFamily: 'var(--font-mono)',
                                  fontSize: 9,
                                  color: 'var(--accent)',
                                  letterSpacing: '0.06em',
                                }}>
                                  This device
                                </span>
                              )}
                            </div>
                            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }}>
                              {s.ip ? `${s.ip} · ` : ''}{timeLabel}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </PfSection>
              )}
              {authSessions !== null && authSessions.length === 0 && (
                <PfSection title="Active sessions">
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>No active sessions</span>
                </PfSection>
              )}

              <PfSection title="Authentication">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="Password" sub="Set or update your sign-in password">
                    <button className="cw-btn cw-btn-sm">Change password</button>
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="Two-factor authentication" sub="Add an extra layer of verification to your account">
                    {mfaMethods === null ? null : mfaMethods.length > 0 ? (
                      <Chip accent>Enabled</Chip>
                    ) : (
                      <button className="cw-btn cw-btn-sm">Enable</button>
                    )}
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="Passkeys" sub="Sign in without a password — use your device’s biometrics">
                    <button className="cw-btn cw-btn-sm">Add passkey</button>
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="Sign out" sub="Clear the local session and return to the sign-in page">
                    <button
                      className="cw-btn cw-btn-sm"
                      style={{ color: 'var(--accent)' }}
                      onClick={handleLogout}
                    >
                      Sign out
                    </button>
                  </PfRow>
                </div>
              </PfSection>

            </div>
          )}

          {/* ══ 通知 ════════════════════════════════════════════ */}
          {tab === 'notifications' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

              {/* Event notifications — real data from api.notifications.preferences() */}
              <PfSection title="Event notifications">
                {notifPrefsErr ? (
                  <div style={{ fontSize: 12.5, color: 'var(--dim)', padding: '6px 0' }}>{notifPrefsErr}</div>
                ) : notifPrefs.length === 0 ? (
                  <div style={{ fontSize: 12.5, color: 'var(--dim)', padding: '6px 0' }}>Loading…</div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <div style={{
                      display: 'grid',
                      gridTemplateColumns: '1fr 58px 58px',
                      padding: '5px 4px 9px',
                      borderBottom: '1px solid var(--line-2)',
                    }}>
                      <span className="cw-label">Event</span>
                      <span className="cw-label" style={{ textAlign: 'center' }}>Email</span>
                      <span className="cw-label" style={{ textAlign: 'center' }}>Push</span>
                    </div>
                    {notifPrefs.map((n) => (
                      <div key={n.event_key} style={{
                        display: 'grid',
                        gridTemplateColumns: '1fr 58px 58px',
                        padding: '11px 4px',
                        borderBottom: '1px solid var(--line-2)',
                        alignItems: 'center',
                      }}>
                        <div>
                          <div style={{ fontSize: 13, fontWeight: 500 }}>{n.label}</div>
                          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }}>
                            {n.sub}
                          </div>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'center' }}>
                          <PfToggle on={n.email_enabled} onChange={() => toggleNotif(n.event_key, 'email_enabled')} />
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'center' }}>
                          <PfToggle on={n.push_enabled} onChange={() => toggleNotif(n.event_key, 'push_enabled')} />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </PfSection>

              {/* Weekly digest — uses real email from api.auth.me(), digest toggle is UI-only for now */}
              <PfSection title="Weekly digest">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="Send weekly report" sub="Summary of tasks, spend and workers — Mondays at 9 AM">
                    <PfToggle on={digestOn} onChange={() => setDigest((v) => !v)} />
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="Digest recipient" sub="Digest emails are sent to">
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--ink-2)' }}>
                      {email}
                    </span>
                  </PfRow>
                </div>
              </PfSection>

            </div>
          )}

        </div>
      </div>
    </div>
  );
}
