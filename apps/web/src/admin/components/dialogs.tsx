import { createContext, useCallback, useContext, useRef, useState } from 'react';
import { Dialog } from './Dialog';

// ─── Imperative confirm / prompt / toast ──────────────────────────────────────
//
// Drop-in replacements for window.confirm / window.prompt / alert that render
// in-app dialogs instead of native browser chrome (which can't be styled, is
// inconsistent across browsers, and blocks the main thread).
//
//   const confirm = useConfirm();
//   if (!(await confirm({ message: 'Delete this item?', tone: 'danger' }))) return;
//
//   const prompt = usePrompt();
//   const name = await prompt({ title: 'New department', label: 'Name' });
//   if (name === null) return;          // cancelled
//
//   const toast = useToast();
//   toast.success('Saved');

// ─── Types ─────────────────────────────────────────────────────────────────────

interface ConfirmOptions {
  title?: string;
  message: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  tone?: 'default' | 'danger';
}

interface PromptOptions {
  title?: string;
  label?: string;
  /** Supplementary description rendered above the input. */
  description?: React.ReactNode;
  defaultValue?: string;
  placeholder?: string;
  confirmText?: string;
  cancelText?: string;
  /** When 'textarea', renders a multi-line input. */
  multiline?: boolean;
  /** Require a non-empty (trimmed) value before confirm is enabled. */
  required?: boolean;
  /** Return an error string to block submission, or null to allow it. */
  validate?: (value: string) => string | null;
}

interface Toast {
  id: number;
  message: string;
  kind: 'success' | 'error' | 'info';
}

interface DialogContextValue {
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
  prompt: (opts: PromptOptions) => Promise<string | null>;
  toast: {
    success: (message: string) => void;
    error: (message: string) => void;
    info: (message: string) => void;
  };
}

const DialogContext = createContext<DialogContextValue | null>(null);

// ─── Confirm dialog ────────────────────────────────────────────────────────────

function ConfirmDialog({
  opts,
  onResult,
}: {
  opts: ConfirmOptions;
  onResult: (ok: boolean) => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const danger = opts.tone === 'danger';
  return (
    <Dialog
      title={opts.title ?? 'Confirm action'}
      onClose={() => onResult(false)}
      initialFocusRef={confirmRef}
    >
      <div className="text-[13px] text-ink whitespace-pre-line leading-relaxed">{opts.message}</div>
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={() => onResult(false)}
          className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2 focus:outline-none focus:border-accent"
        >
          {opts.cancelText ?? 'Cancel'}
        </button>
        <button
          ref={confirmRef}
          type="button"
          onClick={() => onResult(true)}
          className={`px-3 py-1.5 text-[13px] rounded-sm text-paper focus:outline-none ${
            danger ? 'bg-accent hover:opacity-90' : 'bg-ink hover:opacity-90'
          }`}
        >
          {opts.confirmText ?? 'OK'}
        </button>
      </div>
    </Dialog>
  );
}

// ─── Prompt dialog ─────────────────────────────────────────────────────────────

function PromptDialog({
  opts,
  onResult,
}: {
  opts: PromptOptions;
  onResult: (value: string | null) => void;
}) {
  const [value, setValue] = useState(opts.defaultValue ?? '');
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement & HTMLTextAreaElement>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (opts.required && !trimmed) {
      setError('This field is required');
      return;
    }
    if (opts.validate) {
      const err = opts.validate(value);
      if (err) {
        setError(err);
        return;
      }
    }
    onResult(value);
  };

  const InputTag = opts.multiline ? 'textarea' : 'input';

  return (
    <Dialog title={opts.title ?? 'Input'} onClose={() => onResult(null)} initialFocusRef={inputRef}>
      <form onSubmit={submit} className="flex flex-col gap-3">
        {opts.description && (
          <div className="text-[12px] text-dim whitespace-pre-line leading-relaxed">
            {opts.description}
          </div>
        )}
        <div className="flex flex-col gap-1">
          {opts.label && <label className="text-[11px] text-dim font-mono">{opts.label}</label>}
          <InputTag
            ref={inputRef as never}
            value={value}
            rows={opts.multiline ? 4 : undefined}
            onChange={(e) => {
              setValue(e.target.value);
              if (error) setError(null);
            }}
            placeholder={opts.placeholder}
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent resize-y"
          />
          {error && <div className="text-[11px] text-accent">{error}</div>}
        </div>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => onResult(null)}
            className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2 focus:outline-none focus:border-accent"
          >
            {opts.cancelText ?? 'Cancel'}
          </button>
          <button
            type="submit"
            className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 focus:outline-none"
          >
            {opts.confirmText ?? 'OK'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ─── Toasts ────────────────────────────────────────────────────────────────────

let toastCounter = 0;

function ToastStack({ toasts }: { toasts: Toast[] }) {
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-4 left-1/2 -translate-x-1/2 z-[60] flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`px-3 py-2 rounded-md text-[12px] shadow-lg border ${
            t.kind === 'success'
              ? 'bg-surface border-line text-ink'
              : t.kind === 'error'
                ? 'bg-accent-soft border-accent text-accent'
                : 'bg-surface border-line text-dim'
          }`}
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}

// ─── Provider ──────────────────────────────────────────────────────────────────

export function DialogProvider({ children }: { children: React.ReactNode }) {
  const [confirmState, setConfirmState] = useState<{
    opts: ConfirmOptions;
    resolve: (ok: boolean) => void;
  } | null>(null);
  const [promptState, setPromptState] = useState<{
    opts: PromptOptions;
    resolve: (value: string | null) => void;
  } | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const confirm = useCallback(
    (opts: ConfirmOptions) =>
      new Promise<boolean>((resolve) => setConfirmState({ opts, resolve })),
    [],
  );

  const prompt = useCallback(
    (opts: PromptOptions) =>
      new Promise<string | null>((resolve) => setPromptState({ opts, resolve })),
    [],
  );

  const pushToast = useCallback((message: string, kind: Toast['kind']) => {
    const id = ++toastCounter;
    setToasts((prev) => [...prev, { id, message, kind }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3000);
  }, []);

  const toast = useRef({
    success: (m: string) => pushToast(m, 'success'),
    error: (m: string) => pushToast(m, 'error'),
    info: (m: string) => pushToast(m, 'info'),
  }).current;

  return (
    <DialogContext.Provider value={{ confirm, prompt, toast }}>
      {children}
      {confirmState && (
        <ConfirmDialog
          opts={confirmState.opts}
          onResult={(ok) => {
            confirmState.resolve(ok);
            setConfirmState(null);
          }}
        />
      )}
      {promptState && (
        <PromptDialog
          opts={promptState.opts}
          onResult={(value) => {
            promptState.resolve(value);
            setPromptState(null);
          }}
        />
      )}
      <ToastStack toasts={toasts} />
    </DialogContext.Provider>
  );
}

// ─── Hooks ─────────────────────────────────────────────────────────────────────

function useDialogContext(): DialogContextValue {
  const ctx = useContext(DialogContext);
  if (!ctx) throw new Error('useConfirm/usePrompt/useToast must be used within <DialogProvider>');
  return ctx;
}

export function useConfirm() {
  return useDialogContext().confirm;
}

export function usePrompt() {
  return useDialogContext().prompt;
}

export function useToast() {
  return useDialogContext().toast;
}
