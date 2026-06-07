import { useEffect, useRef, useState } from 'react';
import { renderAsync } from 'docx-preview';

/**
 * docx-preview embeds images/fonts as `blob:` object URLs and never revokes
 * them — clearing innerHTML alone orphans (leaks) them. Walk the rendered tree
 * and revoke every blob: URL we find before tearing it down.
 */
function revokeBlobUrls(el: HTMLElement) {
  const urls = new Set<string>();
  el.querySelectorAll<HTMLImageElement>('img[src^="blob:"]').forEach((n) => urls.add(n.src));
  el.querySelectorAll<HTMLLinkElement>('link[href^="blob:"]').forEach((n) => urls.add(n.href));
  // Inline background-image: url(blob:...) on any element.
  el.querySelectorAll<HTMLElement>('[style*="blob:"]').forEach((n) => {
    const m = n.getAttribute('style')?.match(/url\((["']?)(blob:[^"')]+)\1\)/);
    if (m) urls.add(m[2]);
  });
  urls.forEach((u) => {
    try {
      URL.revokeObjectURL(u);
    } catch {
      // ignore — already revoked or not a blob URL
    }
  });
}

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
      // Revoke embedded blob: URLs before dropping the DOM, then clear it so
      // the large rendered tree is eligible for GC on close.
      revokeBlobUrls(el);
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
      {/* Tall viewport-relative cap so most of the panel is used; the full doc
          is rendered, scroll to read it all (docx-preview has no pagination). */}
      <div
        ref={containerRef}
        className="cw-scroll overflow-auto bg-paper-2 flex flex-col items-center py-3"
        style={{
          display: status === 'done' ? undefined : 'none',
          maxHeight: 'calc(100vh - 160px)',
        }}
      />
    </div>
  );
}
