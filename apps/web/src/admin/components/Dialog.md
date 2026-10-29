# Dialog conventions

`Dialog` (`./Dialog.tsx`) is the shared base for every modal in the admin console. It already provides the backdrop (`bg-black/40`), ESC to close, background scroll lock, initial focus, and accessibility attributes such as `role="dialog"` / `aria-modal`. Callers must **not re-implement** any of these.

## 1. Convention

Every "create / edit entity" form in the admin console must be hosted in a `Dialog`:

- Don't lay create / edit forms out inline in a page section.
- Don't hand-roll a `fixed inset-0 ... bg-black/...` overlay + onClick-to-close + custom ESC handling. Dialog already does all of that.

Key points of the `Dialog` API (`Dialog.tsx` is the source of truth — don't guess):

- `title: string` (required) — rendered as the small mono uppercase heading at the top.
- `onClose: () => void`。
- `dismissable?: boolean` (default `true`) — pass `false` while an async action is in flight to block closing via backdrop / ESC.
- `initialFocusRef?`, `className?` (default `'w-full max-w-md'`, controls width), `children`.
- **No `open` prop**: control visibility with conditional rendering, `{open && <Dialog .../>}`.
- **No `footer` prop**: put the cancel / submit buttons at the bottom of `children`.

## 2. Exceptions

`Dialog` is not required in these cases:

- **(a) Inline table-row editing**: clicking "Edit" to make a row's cells editable in place is fine; no modal needed.
- **(b) Read-only / detail overlays**: `Dialog` is not required.
- **(c) Confirmations / text prompts**: use `useConfirm` (confirmations) and `usePrompt` (text input prompts) from `./dialogs`; do **not** hand-roll a confirm modal with `Dialog`.

## 3. Standard usage

State controls visibility + conditional rendering. The form goes in `children`; use `dismissable={!busy}` to lock closing while an async action runs:

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
      setError('Invite failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="Invite user" onClose={onClose} dismissable={!busy} className="w-full max-w-[440px]">
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
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? 'Inviting…' : 'Send invite'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// Caller: state + conditional rendering controls visibility
{showInvite && <InviteUserDialog onClose={() => setShowInvite(false)} onInvited={refresh} />}
```

Key points:

- Visibility is controlled by the caller's state (e.g. `showInvite`) + `{showInvite && <Dialog .../>}`; `Dialog` itself doesn't manage visibility.
- Put buttons at the bottom of the `<form>` / `children`, reusing existing page class styles such as `cw-btn` / `border-line` / `bg-accent`.
- Backdrop / ESC / scroll lock / focus are all provided by `Dialog`; callers must not add them again.

## 4. Reference files that already use Dialog correctly

- `apps/web/src/admin/pages/ApiPlatformPage.tsx`
- `apps/web/src/admin/pages/TenantsPage.tsx`(CreateTenantForm — create tenant)
- `apps/web/src/admin/pages/SsoPage.tsx`
- `apps/web/src/admin/pages/OrgStructurePage.tsx`(move node / add member)
- `apps/web/src/admin/pages/UsersPage.tsx`(InviteUserDialog — invite user, the canonical example)
