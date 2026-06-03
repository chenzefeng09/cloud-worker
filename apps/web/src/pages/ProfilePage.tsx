import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useChatStore } from '../stores/chatStore';
import { useWorkspace } from '../lib/workspaceContext';
import { api } from '../api/client';
import {
  PF_USAGE_WEEK,
  PF_API_KEYS,
  PF_SESSIONS,
  PF_NOTIFS,
  PF_PLAN,
  PF_STATS,
  type NotifSetting,
  type Session,
} from '../lib/designFixtures';

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
  days: { day: string; amount: number }[];
}
function SparkBars({ days }: SparkBarsProps) {
  const m = Math.max(...days.map((d) => d.amount));
  const BAR_H = 60;
  return (
    <div style={{ display: 'flex', gap: 7, height: BAR_H + 22, alignItems: 'flex-end' }}>
      {days.map((d, i) => {
        const isToday = i === days.length - 1;
        return (
          <div
            key={d.day}
            title={`${d.day} · $${d.amount.toFixed(2)}`}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5 }}
          >
            <div style={{
              width: '100%',
              height: `${(d.amount / m) * BAR_H}px`,
              background: isToday ? 'var(--accent)' : 'var(--line)',
              borderRadius: '3px 3px 0 0',
              transition: 'background 120ms',
            }} />
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--dim)' }}>
              {d.day.slice(0, 2)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

interface RingGaugeProps {
  value: number;
  max: number;
  label: string;
  sub: string;
  size?: number;
}
function RingGauge({ value, max, label, sub, size = 90 }: RingGaugeProps) {
  const pct = Math.min(value / max, 1);
  const r = (size - 12) / 2;
  const circ = 2 * Math.PI * r;
  const warn = pct > 0.85;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
      <div style={{ position: 'relative', width: size, height: size }}>
        <svg width={size} height={size} style={{ transform: 'rotate(-90deg)', display: 'block' }}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth={5} />
          <circle
            cx={size / 2} cy={size / 2} r={r} fill="none"
            stroke={warn ? 'var(--warn)' : 'var(--accent)'}
            strokeWidth={5}
            strokeDasharray={`${circ * pct} ${circ * (1 - pct)}`}
            strokeLinecap="round"
          />
        </svg>
        <div style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
        }}>
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 15,
            fontWeight: 700,
            letterSpacing: '-0.03em',
            lineHeight: 1,
          }}>
            ${value.toFixed(2)}
          </span>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 8.5, color: 'var(--dim)' }}>
            of ${max}
          </span>
        </div>
      </div>
      <div style={{ textAlign: 'center' }}>
        <div style={{ fontSize: 12, fontWeight: 600, letterSpacing: '-0.01em' }}>{label}</div>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--dim)' }}>{sub}</div>
      </div>
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

  const displayName = userProfile?.display_name ?? 'You';
  const email       = userProfile?.email ?? 'you@acme.co';
  const initial     = displayName[0]?.toUpperCase() ?? 'Y';

  const [tab,      setTab]      = useState<TabId>('account');
  const [name,     setName]     = useState(displayName);
  const [model,    setModel]    = useState('sonnet 4.5');
  const [autonomy, setAuto]     = useState('assist');
  const [showKey,  setShowKey]  = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>(PF_SESSIONS);
  const [notifs,   setNotifs]   = useState<NotifSetting[]>(PF_NOTIFS);
  const [digestOn, setDigest]   = useState(true);

  const toggleN = (event: string, field: 'email' | 'push') =>
    setNotifs((prev) =>
      prev.map((n) => (n.event === event ? { ...n, [field]: !n[field] } : n)),
    );

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
                <Chip accent>Pro</Chip>
              </div>
              <div style={{
                fontSize: 13,
                color: 'var(--ink-2)',
                marginBottom: 14,
                fontFamily: 'var(--font-mono)',
              }}>
                {email}
              </div>

              {/* stat tiles */}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {[
                  { l: '运行任务数',    v: String(PF_STATS.tasksRun) },
                  { l: '本月 Token',   v: PF_STATS.tokensThisMonth  },
                  { l: '工作节点',      v: String(PF_STATS.workers)  },
                  { l: '注册时间',      v: PF_STATS.memberSince      },
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
                  <PfRow label="默认模型" sub="未指定模板时使用的模型">
                    <SegPicker
                      options={['haiku 3.5', 'sonnet 4.5', 'opus 4']}
                      value={model}
                      onChange={setModel}
                    />
                  </PfRow>
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
                      <Chip>{w.fileCount} 文件</Chip>
                      <Chip accent={w.taskCount > 1}>{w.taskCount} 活跃</Chip>
                    </div>
                  ))}
                </div>
              </PfSection>

            </div>
          )}

          {/* ══ 用量与账单 ══════════════════════════════════════ */}
          {tab === 'usage' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

              {/* gauges + limits */}
              <div style={{
                border: '1px solid var(--line)',
                borderRadius: 'var(--r-md)',
                background: 'var(--surface)',
                padding: '20px 28px',
                display: 'flex',
                alignItems: 'center',
                gap: 32,
              }}>
                <RingGauge
                  value={PF_STATS.todaySpend}
                  max={PF_STATS.dailyCap}
                  label="今日"
                  sub="每日预算"
                />
                <div style={{ width: 1, background: 'var(--line)', alignSelf: 'stretch' }} />
                <RingGauge
                  value={PF_STATS.monthSpend}
                  max={PF_STATS.monthlyCap}
                  label="本月"
                  sub="月度预算"
                />
                <div style={{ flex: 1 }} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 16, alignItems: 'flex-end' }}>
                  {[
                    ['每日上限', `$${PF_STATS.dailyCap}`],
                    ['月度上限', `$${PF_STATS.monthlyCap}`],
                  ].map(([lbl, v]) => (
                    <div key={lbl} style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-end' }}>
                      <span className="cw-label">{lbl}</span>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <span style={{
                          fontFamily: 'var(--font-mono)',
                          fontSize: 20,
                          fontWeight: 700,
                          letterSpacing: '-0.04em',
                        }}>
                          {v}
                        </span>
                        <button className="cw-btn cw-btn-sm">修改</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <PfSection title={`本周消费 — $${PF_USAGE_WEEK.reduce((a, d) => a + d.amount, 0).toFixed(2)} 合计`}>
                <SparkBars days={PF_USAGE_WEEK} />
              </PfSection>

              <PfSection title="套餐" action={
                <button className="cw-btn cw-btn-sm" style={{ color: 'var(--accent)' }}>升级 →</button>
              }>
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
                        {PF_PLAN.name}
                      </span>
                      <span style={{
                        fontFamily: 'var(--font-mono)',
                        fontSize: 13,
                        color: 'var(--ink-2)',
                        fontWeight: 600,
                      }}>
                        ${PF_PLAN.pricePerMonth} / 月
                      </span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--dim)' }}>
                        {PF_PLAN.renewsLabel}
                      </span>
                    </div>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {PF_PLAN.features.map((f) => (
                        <Chip key={f}>{f}</Chip>
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
              </PfSection>

            </div>
          )}

          {/* ══ 安全 ════════════════════════════════════════════ */}
          {tab === 'security' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

              <PfSection title="API 密钥" action={
                <button className="cw-btn cw-btn-sm cw-btn-primary">+ 新建密钥</button>
              }>
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  <div style={{
                    display: 'grid',
                    gridTemplateColumns: '1.3fr 1fr 0.65fr 0.65fr auto',
                    gap: '6px 14px',
                    padding: '5px 4px 9px',
                    borderBottom: '1px solid var(--line-2)',
                  }}>
                    {['名称', '前缀', '创建时间', '最近使用', ''].map((h) => (
                      <span key={h} className="cw-label">{h}</span>
                    ))}
                  </div>
                  {PF_API_KEYS.map((k) => (
                    <div key={k.name} style={{
                      display: 'grid',
                      gridTemplateColumns: '1.3fr 1fr 0.65fr 0.65fr auto',
                      gap: '6px 14px',
                      padding: '10px 4px',
                      borderBottom: '1px solid var(--line-2)',
                      alignItems: 'center',
                    }}>
                      <div>
                        <div style={{ fontSize: 13, fontWeight: 500 }}>{k.name}</div>
                        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--dim)' }}>
                          {k.scopes}
                        </div>
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11.5, color: 'var(--ink-2)' }}>
                          {showKey === k.name ? 'cw_live_4xKm3••••••' : k.prefix}
                        </span>
                        <span
                          onClick={() => setShowKey(showKey === k.name ? null : k.name)}
                          style={{ fontSize: 10, color: 'var(--dim)', cursor: 'pointer', userSelect: 'none' }}
                        >
                          {showKey === k.name ? '隐藏' : '显示'}
                        </span>
                      </div>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }}>
                        {k.created}
                      </span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ok)' }}>
                        {k.lastUsed}
                      </span>
                      <button
                        className="cw-btn cw-btn-sm cw-btn-ghost"
                        style={{ fontSize: 11, color: 'var(--dim)', padding: '3px 8px' }}
                      >
                        撤销
                      </button>
                    </div>
                  ))}
                </div>
              </PfSection>

              <PfSection title="活跃会话" action={
                <button
                  className="cw-btn cw-btn-sm"
                  style={{ color: 'var(--accent)' }}
                  onClick={() => setSessions((p) => p.filter((s) => s.current))}
                >
                  登出其他设备
                </button>
              }>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {sessions.map((s) => (
                    <div key={s.device + s.time} style={{
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
                        {s.device.includes('iOS') ? '▣' : '□'}
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
                          {s.location} · {s.time}
                        </div>
                      </div>
                      {!s.current && (
                        <button
                          className="cw-btn cw-btn-sm cw-btn-ghost"
                          style={{ fontSize: 11, color: 'var(--dim)' }}
                          onClick={() => setSessions((p) => p.filter((x) => x.device !== s.device || x.time !== s.time))}
                        >
                          登出
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </PfSection>

              <PfSection title="身份验证">
                <div style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
                  <PfRow label="密码" sub="3 个月前修改">
                    <button className="cw-btn cw-btn-sm">修改密码</button>
                  </PfRow>
                  <div className="cw-divider" />
                  <PfRow label="双因素认证" sub="已绑定身份验证器应用">
                    <Chip accent>已启用</Chip>
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

              <PfSection title="事件通知">
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
                  {notifs.map((n) => (
                    <div key={n.event} style={{
                      display: 'grid',
                      gridTemplateColumns: '1fr 58px 58px',
                      padding: '11px 4px',
                      borderBottom: '1px solid var(--line-2)',
                      alignItems: 'center',
                    }}>
                      <div>
                        <div style={{ fontSize: 13, fontWeight: 500 }}>{n.event}</div>
                        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }}>
                          {n.sub}
                        </div>
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'center' }}>
                        <PfToggle on={n.email} onChange={() => toggleN(n.event, 'email')} />
                      </div>
                      <div style={{ display: 'flex', justifyContent: 'center' }}>
                        <PfToggle on={n.push} onChange={() => toggleN(n.event, 'push')} />
                      </div>
                    </div>
                  ))}
                </div>
              </PfSection>

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
