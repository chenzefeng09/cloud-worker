import { useEffect, useRef, useState } from 'react';

export default function PptxPreview({ blob }: { blob: Blob }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<'loading' | 'done' | 'error'>('loading');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let cancelled = false;
    let viewer: { preview: (buf: ArrayBuffer) => Promise<unknown>; destroy: () => void } | null = null;
    setStatus('loading');
    setError(null);
    el.innerHTML = '';

    (async () => {
      const { init } = await import('pptx-preview');
      if (cancelled) return;
      const buf = await blob.arrayBuffer();
      if (cancelled) return;
      viewer = init(el, { width: 960, height: 540 });
      await viewer.preview(buf);
      if (!cancelled) setStatus('done');
    })().catch((e) => {
      if (cancelled) return;
      setError(e instanceof Error ? e.message : '未知错误');
      setStatus('error');
    });

    return () => {
      cancelled = true;
      viewer?.destroy();
      if (el) el.innerHTML = '';
    };
  }, [blob]);

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="cw-label px-4 py-2 border-b border-line-2">演示文稿预览</div>
      {status === 'loading' && (
        <div className="px-4 py-3 font-mono text-[11px] text-dim">渲染演示文稿…</div>
      )}
      {status === 'error' && (
        <div className="px-4 py-3 font-mono text-[11px] text-accent">PPT 解析失败：{error}</div>
      )}
      <div
        ref={containerRef}
        className="cw-scroll overflow-auto bg-paper-2 py-3 px-3 flex flex-col items-center"
        style={{
          display: status === 'done' ? undefined : 'none',
          maxHeight: 'calc(100vh - 160px)',
        }}
      />
    </div>
  );
}
