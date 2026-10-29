import { useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useToast } from '../components/dialogs';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

interface ConfigField {
  key: string;
  label: string;
  description: string;
  type: 'number' | 'text' | 'cron';
  default: string | number;
}

const WORKSPACE_FIELDS: ConfigField[] = [
  {
    key: 'workspace.version_retention_days',
    label: 'Version retention (days)',
    description: 'Non-latest versions older than this are cleaned up automatically',
    type: 'number',
    default: 90,
  },
  {
    key: 'workspace.max_versions_per_file',
    label: 'Max versions per file',
    description: 'Maximum number of historical versions kept per file (oldest evicted first)',
    type: 'number',
    default: 20,
  },
  {
    key: 'workspace.cleanup_cron',
    label: 'Cleanup schedule',
    description: 'When the version cleanup job runs',
    type: 'cron',
    default: '0 3 * * *',
  },
];

// ---------------------------------------------------------------------------
// Visual cron editor
// ---------------------------------------------------------------------------

type CronFreq = 'daily' | 'weekly' | 'monthly';
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const;
const WEEKDAY_CRON = [1, 2, 3, 4, 5, 6, 0]; // cron 0=Sun

function parseCron(expr: string): { freq: CronFreq; minute: number; hour: number; monthDay: number; weekDays: number[] } {
  const parts = expr.trim().split(/\s+/);
  const minute = Number(parts[0]) || 0;
  const hour = Number(parts[1]) || 0;
  const dom = parts[2] ?? '*';
  const dow = parts[4] ?? '*';

  if (dom !== '*') {
    return { freq: 'monthly', minute, hour, monthDay: Number(dom) || 1, weekDays: [] };
  }
  if (dow !== '*') {
    const days = dow.split(',').map(Number).filter((n) => !isNaN(n));
    return { freq: 'weekly', minute, hour, monthDay: 1, weekDays: days };
  }
  return { freq: 'daily', minute, hour, monthDay: 1, weekDays: [] };
}

function toCron(freq: CronFreq, minute: number, hour: number, monthDay: number, weekDays: number[]): string {
  if (freq === 'monthly') return `${minute} ${hour} ${monthDay} * *`;
  if (freq === 'weekly') {
    const d = weekDays.length ? weekDays.sort((a, b) => a - b).join(',') : '1';
    return `${minute} ${hour} * * ${d}`;
  }
  return `${minute} ${hour} * * *`;
}

function describeCron(freq: CronFreq, minute: number, hour: number, monthDay: number, weekDays: number[]): string {
  const t = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  if (freq === 'daily') return `Runs daily at ${t}`;
  if (freq === 'monthly') return `Runs on day ${monthDay} of each month at ${t}`;
  const map: Record<number, string> = { 0: 'Sun', 1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat' };
  const names = weekDays.sort((a, b) => a - b).map((d) => map[d]);
  return `Runs every ${names.join(', ')} at ${t}`;
}

const selectCls =
  'rounded border border-dim/30 bg-transparent px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-accent appearance-none cursor-pointer';

function CronEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const parsed = useMemo(() => parseCron(value), [value]);
  const [freq, setFreq] = useState<CronFreq>(parsed.freq);
  const [hour, setHour] = useState(parsed.hour);
  const [minute, setMinute] = useState(parsed.minute);
  const [monthDay, setMonthDay] = useState(parsed.monthDay);
  const [weekDays, setWeekDays] = useState<number[]>(parsed.weekDays);

  useEffect(() => {
    const p = parseCron(value);
    setFreq(p.freq);
    setHour(p.hour);
    setMinute(p.minute);
    setMonthDay(p.monthDay);
    setWeekDays(p.weekDays);
  }, [value]);

  const emit = (f: CronFreq, h: number, m: number, md: number, wd: number[]) => {
    onChange(toCron(f, m, h, md, wd));
  };

  const toggleDay = (d: number) => {
    const next = weekDays.includes(d) ? weekDays.filter((x) => x !== d) : [...weekDays, d];
    setWeekDays(next);
    emit(freq, hour, minute, monthDay, next);
  };

  return (
    <div className="space-y-3">
      {/* Frequency tabs */}
      <div className="flex gap-0 rounded-md border border-dim/30 w-fit overflow-hidden">
        {([['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly']] as const).map(([f, label]) => (
          <button
            key={f}
            type="button"
            className={`px-3 py-1 text-sm transition-colors ${
              freq === f
                ? 'bg-accent text-white'
                : 'bg-transparent hover:bg-dim/10'
            }`}
            onClick={() => { setFreq(f); emit(f, hour, minute, monthDay, weekDays); }}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Weekly: day checkboxes */}
      {freq === 'weekly' && (
        <div className="flex gap-1.5 flex-wrap">
          {WEEKDAYS.map((name, i) => {
            const cronVal = WEEKDAY_CRON[i];
            const active = weekDays.includes(cronVal);
            return (
              <button
                key={cronVal}
                type="button"
                className={`w-8 h-8 rounded text-xs font-medium transition-colors ${
                  active
                    ? 'bg-accent text-white'
                    : 'border border-dim/30 hover:bg-dim/10'
                }`}
                onClick={() => toggleDay(cronVal)}
              >
                {name}
              </button>
            );
          })}
        </div>
      )}

      {/* Monthly: day selector */}
      {freq === 'monthly' && (
        <div className="flex items-center gap-2 text-sm">
          <span>On day</span>
          <select
            className={selectCls}
            value={monthDay}
            onChange={(e) => { const v = Number(e.target.value); setMonthDay(v); emit(freq, hour, minute, v, weekDays); }}
          >
            {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
          <span>of each month</span>
        </div>
      )}

      {/* Time picker */}
      <div className="flex items-center gap-2 text-sm">
        <span>Run at</span>
        <select
          className={selectCls}
          value={hour}
          onChange={(e) => { const v = Number(e.target.value); setHour(v); emit(freq, v, minute, monthDay, weekDays); }}
        >
          {Array.from({ length: 24 }, (_, i) => (
            <option key={i} value={i}>{String(i).padStart(2, '0')}</option>
          ))}
        </select>
        <span>:</span>
        <select
          className={selectCls}
          value={minute}
          onChange={(e) => { const v = Number(e.target.value); setMinute(v); emit(freq, hour, v, monthDay, weekDays); }}
        >
          {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => (
            <option key={m} value={m}>{String(m).padStart(2, '0')}</option>
          ))}
        </select>
      </div>

      {/* Preview */}
      <p className="text-[11px] text-dim">
        {describeCron(freq, minute, hour, monthDay, weekDays)}
        <span className="ml-2 opacity-60">({toCron(freq, minute, hour, monthDay, weekDays)})</span>
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function PlatformSettingsPage() {
  const { selectedTenantId } = useAdminStore();
  const toast = useToast();
  const tid = selectedTenantId ?? undefined;

  const [values, setValues] = useState<Record<string, string | number>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [todo, setTodo] = useState(false);

  const load = async () => {
    setLoading(true);
    setTodo(false);
    try {
      const res = await api.admin.platformConfig.list('workspace', tid);
      const map: Record<string, string | number> = {};
      for (const item of res.items) {
        map[item.key] = typeof item.value === 'object' ? JSON.stringify(item.value) : (item.value as string | number);
      }
      for (const f of WORKSPACE_FIELDS) {
        if (!(f.key in map)) map[f.key] = f.default;
      }
      setValues(map);
    } catch (err) {
      if (isTodo(err)) {
        setTodo(true);
      } else {
        toast.error('Failed to load settings');
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [tid]);

  const saveCron = async () => {
    const field = WORKSPACE_FIELDS.find((f) => f.key === 'workspace.cleanup_cron')!;
    const value = values[field.key] ?? field.default;
    setSaving(field.key);
    try {
      await api.admin.platformConfig.set(field.key, value, tid);
      toast.success(`${field.label} saved`);
    } catch {
      toast.error(`Failed to save ${field.label}`);
    } finally {
      setSaving(null);
    }
  };

  const saveNumber = async (field: ConfigField) => {
    const raw = values[field.key];
    const value = Number(raw);
    if (isNaN(value) || value <= 0) {
      toast.error(`${field.label} must be a positive integer`);
      return;
    }
    setSaving(field.key);
    try {
      await api.admin.platformConfig.set(field.key, value, tid);
      toast.success(`${field.label} saved`);
    } catch {
      toast.error(`Failed to save ${field.label}`);
    } finally {
      setSaving(null);
    }
  };

  if (todo) {
    return (
      <div className="p-6">
        <h2 className="text-lg font-semibold mb-1">Platform settings</h2>
        <p className="text-[12px] text-dim">Endpoint not ready yet (502 / 404)</p>
      </div>
    );
  }

  const numberFields = WORKSPACE_FIELDS.filter((f) => f.type === 'number');
  const cronField = WORKSPACE_FIELDS.find((f) => f.type === 'cron')!;

  return (
    <div className="p-6 max-w-2xl">
      <h2 className="text-lg font-semibold mb-1">Platform settings</h2>
      <p className="text-[12px] text-dim mb-6">Workspace version snapshot policy and cleanup job settings</p>

      {loading ? (
        <p className="text-sm text-dim">Loading…</p>
      ) : (
        <div className="space-y-5">
          <fieldset className="border border-dim/20 rounded-lg p-4">
            <legend className="text-sm font-medium px-2">Workspace version write-back</legend>
            <div className="space-y-4 mt-2">
              {numberFields.map((f) => (
                <div key={f.key} className="flex items-end gap-3">
                  <div className="flex-1">
                    <label className="block text-sm font-medium mb-1">{f.label}</label>
                    <p className="text-[11px] text-dim mb-1.5">{f.description}</p>
                    <input
                      type="number"
                      className="w-full rounded border border-dim/30 bg-transparent px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-accent"
                      value={values[f.key] ?? f.default}
                      min={1}
                      onChange={(e) =>
                        setValues((prev) => ({ ...prev, [f.key]: e.target.valueAsNumber || '' }))
                      }
                    />
                  </div>
                  <button
                    className="shrink-0 rounded bg-accent px-3 py-1.5 text-sm text-white hover:opacity-90 disabled:opacity-50"
                    disabled={saving === f.key}
                    onClick={() => saveNumber(f)}
                  >
                    {saving === f.key ? 'Saving…' : 'Save'}
                  </button>
                </div>
              ))}
            </div>
          </fieldset>

          <fieldset className="border border-dim/20 rounded-lg p-4">
            <legend className="text-sm font-medium px-2">{cronField.label}</legend>
            <div className="mt-2">
              <p className="text-[11px] text-dim mb-3">{cronField.description}</p>
              <CronEditor
                value={String(values[cronField.key] ?? cronField.default)}
                onChange={(v) => setValues((prev) => ({ ...prev, [cronField.key]: v }))}
              />
              <div className="mt-3 flex justify-end">
                <button
                  className="rounded bg-accent px-3 py-1.5 text-sm text-white hover:opacity-90 disabled:opacity-50"
                  disabled={saving === cronField.key}
                  onClick={saveCron}
                >
                  {saving === cronField.key ? 'Saving…' : 'Save'}
                </button>
              </div>
            </div>
          </fieldset>
        </div>
      )}
    </div>
  );
}
