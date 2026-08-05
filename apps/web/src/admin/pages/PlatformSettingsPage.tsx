import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useToast } from '../components/dialogs';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

interface ConfigField {
  key: string;
  label: string;
  description: string;
  type: 'number' | 'text';
  default: string | number;
}

const WORKSPACE_FIELDS: ConfigField[] = [
  {
    key: 'workspace.version_retention_days',
    label: '版本保留天数',
    description: '非最新版本超过此天数将被自动清理',
    type: 'number',
    default: 90,
  },
  {
    key: 'workspace.max_versions_per_file',
    label: '每文件最大版本数',
    description: '单个文件保留的历史版本上限（FIFO 淘汰）',
    type: 'number',
    default: 20,
  },
  {
    key: 'workspace.cleanup_cron',
    label: '清理任务时间',
    description: 'Cron 表达式，默认每日 03:00 执行（0 3 * * *）',
    type: 'text',
    default: '0 3 * * *',
  },
];

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
        toast.error('加载配置失败');
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [tid]);

  const save = async (field: ConfigField) => {
    const raw = values[field.key];
    const value = field.type === 'number' ? Number(raw) : raw;
    if (field.type === 'number' && (isNaN(value as number) || (value as number) <= 0)) {
      toast.error(`${field.label} 必须为正整数`);
      return;
    }
    setSaving(field.key);
    try {
      await api.admin.platformConfig.set(field.key, value, tid);
      toast.success(`${field.label} 已保存`);
    } catch {
      toast.error(`保存 ${field.label} 失败`);
    } finally {
      setSaving(null);
    }
  };

  if (todo) {
    return (
      <div className="p-6">
        <h2 className="text-lg font-semibold mb-1">平台设置</h2>
        <p className="text-[12px] text-dim">端点尚未就绪（502 / 404）</p>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-2xl">
      <h2 className="text-lg font-semibold mb-1">平台设置</h2>
      <p className="text-[12px] text-dim mb-6">工作区版本快照策略与清理任务配置</p>

      {loading ? (
        <p className="text-sm text-dim">加载中…</p>
      ) : (
        <div className="space-y-5">
          <fieldset className="border border-dim/20 rounded-lg p-4">
            <legend className="text-sm font-medium px-2">工作区版本回写</legend>
            <div className="space-y-4 mt-2">
              {WORKSPACE_FIELDS.map((f) => (
                <div key={f.key} className="flex items-end gap-3">
                  <div className="flex-1">
                    <label className="block text-sm font-medium mb-1">{f.label}</label>
                    <p className="text-[11px] text-dim mb-1.5">{f.description}</p>
                    <input
                      type={f.type === 'number' ? 'number' : 'text'}
                      className="w-full rounded border border-dim/30 bg-transparent px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-accent"
                      value={values[f.key] ?? f.default}
                      min={f.type === 'number' ? 1 : undefined}
                      onChange={(e) =>
                        setValues((prev) => ({
                          ...prev,
                          [f.key]: f.type === 'number' ? e.target.valueAsNumber || '' : e.target.value,
                        }))
                      }
                    />
                  </div>
                  <button
                    className="shrink-0 rounded bg-accent px-3 py-1.5 text-sm text-white hover:opacity-90 disabled:opacity-50"
                    disabled={saving === f.key}
                    onClick={() => save(f)}
                  >
                    {saving === f.key ? '保存中…' : '保存'}
                  </button>
                </div>
              ))}
            </div>
          </fieldset>
        </div>
      )}
    </div>
  );
}
