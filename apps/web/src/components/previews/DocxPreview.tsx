import { useEffect, useRef, useState } from 'react';
import { renderAsync } from 'docx-preview';

export default function DocxPreview({ blob }: { blob: Blob }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'loading' | 'done' | 'error'>('loading');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let cancelled = false;
    setStatus('loading');
    setError(null);
    el.innerHTML = '';

    renderAsync(blob, el, undefined, {
      className: 'docx-preview',
      inWrapper: true,
      ignoreWidth: false,
      ignoreHeight: false,
    })
      .then(() => {
        if (!cancelled) setStatus('done');
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : '渲染失败');
        setStatus('error');
      });

    return () => {
      cancelled = true;
      el.innerHTML = '';
    };
  }, [blob]);

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="cw-label px-4 py-2 border-b border-line-2">文档预览</div>
      {status === 'loading' && (
        <div className="px-4 py-3 font-mono text-[11px] text-dim">渲染文档…</div>
      )}
      {status === 'error' && (
        <div className="px-4 py-3 font-mono text-[11px] text-accent">文档渲染失败：{error}</div>
      )}
      <div
        ref={containerRef}
        className="cw-scroll overflow-auto max-h-[640px] bg-paper-2 flex justify-center py-3"
        style={{ display: status === 'done' ? undefined : 'none' }}
      />
    </div>
  );
}
