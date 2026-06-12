# Dialog 弹窗约定

`Dialog`（`./Dialog.tsx`）是 admin 后台所有模态弹窗的统一基座。它已内置 backdrop（`bg-black/40`）、ESC 关闭、背景滚动锁、初始 focus、`role="dialog"` / `aria-modal` 等无障碍属性。调用方**不要重复实现**这些。

## 1. 约定

admin 后台所有「创建 / 编辑实体」类表单交互，一律用 `Dialog` 弹窗承载：

- 禁止把创建 / 编辑表单内联铺在页面区块里。
- 禁止再手搓 `fixed inset-0 ... bg-black/...` overlay + onClick 关闭 + 自写 ESC 逻辑。这些 Dialog 已经做了。

`Dialog` 接口要点（以 `Dialog.tsx` 为准，别猜）：

- `title: string`（必填）—— 渲染成顶部 mono 大写小标题。
- `onClose: () => void`。
- `dismissable?: boolean`（默认 `true`）—— 异步进行中传 `false`，禁止 backdrop / ESC 关闭。
- `initialFocusRef?`、`className?`（默认 `'w-full max-w-md'`，控宽用）、`children`。
- **没有 `open` prop**：用 `{open && <Dialog .../>}` 条件渲染控制显隐。
- **没有 `footer` prop**：取消 / 提交按钮直接写进 `children` 底部。

## 2. 例外

以下场景不强制使用 `Dialog`：

- **(a) 表格行内编辑**：点「编辑」让某行单元格就地可编辑，允许保留，不必弹窗。
- **(b) 纯展示 / 详情类 overlay**：不强制走 `Dialog`。
- **(c) 确认 / 文本输入提示**：用 `./dialogs` 提供的 `useConfirm`（确认类）、`usePrompt`（文本输入提示），**不要**用 `Dialog` 手搓 confirm 弹窗。

## 3. 标准用法

state 控显隐 + 条件渲染。表单内容作为 `children`，异步进行中用 `dismissable={!busy}` 锁住关闭：

```tsx
import { useState } from 'react';
import { Dialog } from '../components/Dialog';

function InviteUserDialog({ onClose, onInvited }: { onClose: () => void; onInvited: () => void }) {
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.admin.users.invite({ email });
      onInvited();
      onClose();
    } catch (err) {
      setError('邀请失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="邀请用户" onClose={onClose} dismissable={!busy} className="w-full max-w-[440px]">
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
        <input
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? '邀请中…' : '发送邀请'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// 调用方：用 state + 条件渲染控制显隐
{showInvite && <InviteUserDialog onClose={() => setShowInvite(false)} onInvited={refresh} />}
```

要点：

- 显隐由调用方的 state（如 `showInvite`）+ `{showInvite && <Dialog .../>}` 控制，`Dialog` 自身不管显隐。
- 按钮放在 `<form>` / `children` 底部，沿用 `cw-btn` / `border-line` / `bg-accent` 等页面既有类名风格。
- backdrop / ESC / 滚动锁 / focus 全由 `Dialog` 提供，调用方不要再写。

## 4. 当前已正确复用 Dialog 的参考文件

- `apps/web/src/admin/pages/ApiPlatformPage.tsx`
- `apps/web/src/admin/pages/TenantsPage.tsx`（CreateTenantForm 创建租户）
- `apps/web/src/admin/pages/SsoPage.tsx`
- `apps/web/src/admin/pages/OrgStructurePage.tsx`（移动节点 / 添加成员）
- `apps/web/src/admin/pages/UsersPage.tsx`（InviteUserDialog 邀请用户，标准样板）
