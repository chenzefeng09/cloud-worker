import { Component, type ErrorInfo, type ReactNode } from 'react';

// ─── Error boundary ────────────────────────────────────────────────────────────
//
// Catches render-time exceptions in the admin route subtree and shows a friendly
// fallback panel (design tokens, English copy) instead of a blank white screen. The
// "Retry" button resets the boundary so a transient error can recover without a
// full page reload.

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface for debugging; the fallback UI is rendered from state.
    console.error('[admin] render error caught by ErrorBoundary', error, info);
  }

  private reset = () => {
    this.setState({ error: null });
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="h-full w-full flex items-center justify-center p-6">
        <div className="border border-line rounded-md bg-surface p-6 max-w-md w-full flex flex-col gap-3">
          <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">Page error</div>
          <div className="text-[15px] font-display text-ink tracking-[-0.02em]">Something went wrong</div>
          <p className="text-[12px] text-dim leading-relaxed">
            An error occurred while rendering this page. Try again, or go to another page to continue.
          </p>
          <div className="border border-line-2 rounded-sm bg-paper-2 px-3 py-2 font-mono text-[11px] text-accent break-words whitespace-pre-wrap max-h-32 overflow-auto">
            {error.message || String(error)}
          </div>
          <div className="flex justify-end pt-1">
            <button
              type="button"
              onClick={this.reset}
              className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 focus:outline-none"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }
}
