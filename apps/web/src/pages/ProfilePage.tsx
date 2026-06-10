import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useChatStore } from '../stores/chatStore';
import { useWorkspace } from '../lib/workspaceContext';
import { api } from '../api/client';
import { formatRelativeTimeZh } from '../lib/format';
import type { ActiveSession, ApiKey, ApiKeyCreated, DailyTokenUsage, MfaMethod, ModelCatalogProvider, ModelPurpose, NotifPreferenceItem, QuotaInfo, TokenUsage, UserModelPreference, UserProfile } from '../api/types';
import { useDefaultModel, clearModelCache } from '../hooks/useDefaultModel';
import { MODEL_OPTIONS, SYSTEM_FALLBACK_MODEL, modelLabel } from '../lib/modelOptions';
import { findProviderForModel, getModelsForTypes } from '../lib/providerModels';

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
}
function PfField({ label, value, onChange, readonly }: PfFieldProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
      <label className="cw-label">{label}</label>
      <input
        value={value}
        onChange={onChange ? (e) => onChange(e.target.value) : undefined}
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

interface SegPickerProps {
  options: string[];
  value: string;
  onChange: (v: string) => void;
}
function SegPicker({ options, value, onChange }: SegPickerProps) {
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {options.map((o) => {
        const active = o === value;
        return (
          <span
            key={o}
            onClick={() => onChange(o)}
            style={{
              padding: '4px 11px',
              borderRadius: 999,
              cursor: 'pointer',
              border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`,
              background: active ? 'var(--accent-soft)' : 'var(--surface)',
              color: active ? 'var(--accent)' : 'var(--ink-2)',
              fontFamily: 'var(--font-mono)',
              fontSize: 10.5,
              fontWeight: active ? 600 : 400,
              transition: 'all 120ms',
            }}
          >
            {o}
          </span>
        );
      })}
    </div>
  );
}

interface SparkBarsProps {
  days: DailyTokenUsage[];
}
function SparkBars({ days }: SparkBarsProps) {
  const m = Math.max(...days.map((d) => d.tokens), 1);  // avoid division by zero
  const BAR_H = 60;
  const allZero = days.every((d) => d.tokens === 0);
  return (
    <div style={{ display: 'flex', gap: 7, height: BAR_H + 22, alignItems: 'flex-end' }}>
      {days.map((d, i) => {
        const isToday = i === days.length - 1;
        const barPx = allZero ? 3 : Math.max((d.tokens / m) * BAR_H, d.tokens > 0 ? 3 : 0);
        return (
          <div
            key={d.day}
            title={`${d.day} · ${d.tokens.toLocaleString()} tokens`}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5 }}
          >
            <div style={{
              width: '100%',
              height: `${barPx}px`,
              background: isToday ? 'var(--accent)' : allZero ? 'var(--line-2)' : 'var(--line)',
              borderRadius: '3px 3px 0 0',
              transition: 'background 120ms',
            }} />
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--dim)' }}>
              {d.day.slice(5)}  {/* show MM-DD */}
            </span>
          </div>
        );
      })}
    </div>
  );
}


// ── Main page ─────────────────────────────────────────────────────

type TabId = 'account' | 'usage' | 'security' | 'notifications';

const TABS: { id: TabId; label: string }[] = [
  { id: 'account',       label: '账户' },
  { id: 'usage',         label: '用量与账单' },
  { id: 'security',      label: '安全' },
  { id: 'notifications', label: '通知' },
];

export function ProfilePage() {
  const navigate = useNavigate();
  const userProfile = useChatStore((s) => s.userProfile);
  const logout      = useChatStore((s) => s.logout);
  const { allWs }   = useWorkspace();

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

  // Daily token usage for the bar chart (last 7 days).
  const [dailyTokens, setDailyTokens] = useState<DailyTokenUsage[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.runs.tokenUsageDaily(7).then((data) => {
      if (!cancelled) setDailyTokens(data.days);
    }).catch(() => { /* best-effort — chart stays hidden */ });
    return () => { cancelled = true; };
  }, []);

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
      setApiKeysError('加载 API 密钥失败');
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
      setApiKeysError('创建 API 密钥失败');
    }
  };

  const handleRevokeKey = async (id: string) => {
    try {
      await api.apiKeys.revoke(id);
      setApiKeys((prev) => prev.filter((k) => k.id !== id));
    } catch {
      setApiKeysError('吊销 API 密钥失败');
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
  const [autonomy, setAuto]     = useState('assist');
  const [digestOn, setDigest]   = useState(true);

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
      setUserPrefError('加载模型偏好失败');
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
      console.warn('加载模型列表失败', err);
      setProvidersError('加载模型列表失败，已使用内置模型列表');
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

  const handleSaveModel = async () => {
    if (!draftModel) return;
    // Resolve the owning provider via type_map + flat models. A model with no
    // resolvable provider must not be saved with an empty provider_id.
    const providerId = findProviderForModel(providers, draftModel, ['llm']);
    if (!providerId) {
      setSaveMsg('无法确定该模型所属的 Provider');
      setTimeout(() => setSaveMsg(null), 3000);
      return;
    }
    setSavingModel(true);
    setSaveMsg(null);
    try {
      await api.modelPreferences.upsert({
        purpose,
        model_id: draftModel,
        provider_id: providerId,
      });
      clearModelCache(purpose);
      draftTouched.current = false; // re-sync draft from the refreshed data
      refetchDefault();
      fetchUserPref();
      setSaveMsg('已保存');
      setTimeout(() => setSaveMsg(null), 2000);
    } catch {
      setSaveMsg('保存失败');
    } finally {
      setSavingModel(false);
    }
  };

  const handleResetModel = async () => {
    setSavingModel(true);
    setSaveMsg(null);
    try {
      await api.modelPreferences.delete(purpose);
      clearModelCache(purpose);
      draftTouched.current = false;
      refetchDefault();
      fetchUserPref();
      setDraftModel(null); // force re-sync from effect
      setSaveMsg('已恢复默认');
      setTimeout(() => setSaveMsg(null), 2000);
    } catch {
      setSaveMsg('恢复失败');
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
      if (!cancelled) setNotifPrefsErr('通知偏好加载失败');
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
          setNotifPrefsErr('保存通知偏好失败');
        });
      }
      return updated;
    });
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
      <PageHead crumbs={[{ label: '首页', to: '/home' }, { label: '个人' }]} />

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

              {/* stat tiles — 运行任务数 and Token 累计 from real API */}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {[
                  { l: '运行任务数', v: runsCount !== null ? String(runsCount) : '…' },
                  {
                    l: 'Token 累计',
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

            <button className="cw-btn cw-btn-sm cw-btn-ghost" onClick={() => setTab('account')}>
              编辑资料
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
                title="个人信息"
                action={<button className="cw-btn cw-btn-sm cw-btn-primary">保存更改</button>}
              >
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
                  <PfField label="显示名称"    value={name}               onChange={setName} />
                  <PfField label="邮箱地址"    value={email}              readonly />
                  <PfField label="时区"        value="Asia / Shanghai (GMT+8)" readonly />
                  <PfField label="语言"        value="简体中文"            readonly />
                </div>
              </PfSection>

              <PfSection title="工作节点默认设置">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="默认模型" sub={
                    userPrefLoading ? '加载中…'
                      : userPref ? `来源：用户偏好 (${modelLabel(userPref.model_id)})`
                      : modelSource === 'tenant' ? '来源：租户默认'
                      : modelSource === 'system' ? '来源：系统默认'
                      : '未指定模板时使用的模型'
                  }>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <select
                        value={draftModel ?? ''}
                        onChange={(e) => {
                          draftTouched.current = true;
                          setDraftModel(e.target.value);
                        }}
                        style={{
                          padding: '5px 10px',
                          border: '1px solid var(--line)',
                          borderRadius: 'var(--r-sm)',
                          fontFamily: 'var(--font-mono)',
                          fontSize: 11,
                          background: 'var(--surface)',
                          color: 'var(--ink)',
                          outline: 'none',
                          cursor: 'pointer',
                          minWidth: 180,
                        }}
                      >
                        {modelOptions.map((o) => (
                          <option key={o.id} value={o.id}>{o.label}</option>
                        ))}
                      </select>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-primary"
                        onClick={handleSaveModel}
                        disabled={savingModel || !draftModel}
                        style={{ fontSize: 11 }}
                      >
                        {savingModel ? '保存中…' : '保存'}
                      </button>
                      {userPref && (
                        <button
                          className="cw-btn cw-btn-sm cw-btn-ghost"
                          onClick={handleResetModel}
                          disabled={savingModel}
                          style={{ fontSize: 11 }}
                        >
                          恢复默认
                        </button>
                      )}
                      {saveMsg && (
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: saveMsg.includes('失败') ? 'var(--accent)' : 'var(--ink-2)' }}>
                          {saveMsg}
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
                  <div className="cw-divider" />
                  <PfRow label="默认自主度" sub="工作节点无需确认即可执行的程度">
                    <SegPicker
                      options={['辅助', '自动', '完全']}
                      value={autonomy}
                      onChange={setAuto}
                    />
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="默认调度" sub="未被模板覆盖时的任务触发方式">
                    <Chip>一次 · 手动</Chip>
                  </PfRow>
                </div>
              </PfSection>

              <PfSection title="工作空间">
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

              {/* gauges + limits — DESIGN FIXTURE (no per-user spend API yet) */}
              {/* 每日 Token 用量柱状图 — real data from GET /runs/token-usage/daily */}
              {dailyTokens === null ? (
                <PfSection title="每日 Token 用量（近 7 天）">
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>加载中…</span>
                </PfSection>
              ) : (
                <PfSection
                  title={`每日 Token 用量（近 7 天）— ${dailyTokens.reduce((a, d) => a + d.tokens, 0).toLocaleString()} tokens 合计`}
                >
                  {dailyTokens.every((d) => d.tokens === 0) ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      <SparkBars days={dailyTokens} />
                      <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)', textAlign: 'center' }}>
                        近 7 天无 Token 用量
                      </span>
                    </div>
                  ) : (
                    <SparkBars days={dailyTokens} />
                  )}
                </PfSection>
              )}

              {/* 套餐 — real data from GET /billing/me */}
              <PfSection title="套餐" action={
                <button className="cw-btn cw-btn-sm" style={{ color: 'var(--accent)' }}>升级 →</button>
              }>
                {billingError ? (
                  <span className="cw-label" style={{ fontStyle: 'italic' }}>套餐数据暂不可用</span>
                ) : billingInfo === null ? (
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>加载中…</span>
                ) : billingInfo.plan === null ? (
                  <span className="cw-label" style={{ fontStyle: 'italic' }}>暂无活跃订阅</span>
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
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flexShrink: 0 }}>
                      <button className="cw-btn cw-btn-sm">账单门户 →</button>
                      <button className="cw-btn cw-btn-sm cw-btn-ghost" style={{ fontSize: 11, color: 'var(--dim)' }}>
                        下载发票
                      </button>
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
              <PfSection title="API 密钥">
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
                      新密钥（仅显示一次，请立即复制保存）
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
                        {copyDone ? '已复制' : '复制'}
                      </button>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-ghost"
                        onClick={() => setCreatedKey(null)}
                      >
                        关闭
                      </button>
                    </div>
                  </div>
                )}

                {/* Create form */}
                <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
                  <input
                    value={newKeyName}
                    onChange={(e) => setNewKeyName(e.target.value)}
                    placeholder="新密钥名称，例如 Production"
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
                    + 新建密钥
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
                    {['名称', '前缀', '创建时间', '最近使用', ''].map((h) => (
                      <span key={h} className="cw-label">{h}</span>
                    ))}
                  </div>

                  {apiKeysLoading && (
                    <div style={{ padding: '12px 4px', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                      加载中…
                    </div>
                  )}

                  {!apiKeysLoading && apiKeys.length === 0 && (
                    <div style={{ padding: '12px 4px', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)', fontStyle: 'italic' }}>
                      暂无 API 密钥
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
                        {formatRelativeTimeZh(k.created_at)}
                      </span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                        {k.last_used_at ? formatRelativeTimeZh(k.last_used_at) : '从未'}
                      </span>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-ghost"
                        style={{ fontSize: 11, color: 'var(--dim)', padding: '3px 8px' }}
                        onClick={() => handleRevokeKey(k.id)}
                      >
                        撤销
                      </button>
                    </div>
                  ))}
                </div>
              </PfSection>

              {/* Active sessions — real data from identity GET /sessions */}
              {authSessions !== null && authSessions.length > 0 && (
                <PfSection title="活跃会话">
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {authSessions.map((s) => {
                      const isMobile = s.device.includes('iOS') || s.device.includes('Android');
                      const timeLabel = s.last_seen_at
                        ? new Date(s.last_seen_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
                        : new Date(s.issued_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
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
                                  当前设备
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
                <PfSection title="活跃会话">
                  <span className="cw-label" style={{ fontStyle: 'italic', color: 'var(--dim)' }}>无活跃会话记录</span>
                </PfSection>
              )}

              <PfSection title="身份验证">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="密码" sub="设置或更新登录密码">
                    <button className="cw-btn cw-btn-sm">修改密码</button>
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="双因素认证" sub="为账户添加额外验证层">
                    {mfaMethods === null ? null : mfaMethods.length > 0 ? (
                      <Chip accent>已启用</Chip>
                    ) : (
                      <button className="cw-btn cw-btn-sm">启用</button>
                    )}
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="通行密钥" sub="无需密码登录 — 使用设备生物识别">
                    <button className="cw-btn cw-btn-sm">添加通行密钥</button>
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="退出登录" sub="清除本地会话并返回登录页">
                    <button
                      className="cw-btn cw-btn-sm"
                      style={{ color: 'var(--accent)' }}
                      onClick={handleLogout}
                    >
                      登出
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
              <PfSection title="事件通知">
                {notifPrefsErr ? (
                  <div style={{ fontSize: 12.5, color: 'var(--dim)', padding: '6px 0' }}>{notifPrefsErr}</div>
                ) : notifPrefs.length === 0 ? (
                  <div style={{ fontSize: 12.5, color: 'var(--dim)', padding: '6px 0' }}>加载中…</div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column' }}>
                    <div style={{
                      display: 'grid',
                      gridTemplateColumns: '1fr 58px 58px',
                      padding: '5px 4px 9px',
                      borderBottom: '1px solid var(--line-2)',
                    }}>
                      <span className="cw-label">事件</span>
                      <span className="cw-label" style={{ textAlign: 'center' }}>邮件</span>
                      <span className="cw-label" style={{ textAlign: 'center' }}>推送</span>
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
              <PfSection title="每周摘要">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="发送每周报告" sub="任务、消费与工作节点汇总 — 每周一上午 9 点">
                    <PfToggle on={digestOn} onChange={() => setDigest((v) => !v)} />
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="摘要收件地址" sub="摘要邮件发送至">
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
