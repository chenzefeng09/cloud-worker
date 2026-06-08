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

    // 整页按比例缩放贴合容器宽度：docx-preview 渲染的是固定 A4 页宽（section.docx），
    // 窗口窄时会被截断出横向滚动。这里只在内容比容器宽时缩小，贴合容器、不放大（放大会
    // 撑乱版式且糊）。缩放对象是单个 section 页面元素，wrapper 的灰背景/padding 单独清掉，
    // 否则灰背景会跟着错位露出灰条。
    const applyScale = () => {
      // docx-preview 用 `${className}-wrapper` 作为外层类名：我们传的 className='docx-preview'，
      // 所以 wrapper 实际是 .docx-preview-wrapper（不是 .docx-wrapper）。用后缀选择器兜底。
      const wrapper = el.querySelector<HTMLElement>('.docx-preview-wrapper')
        ?? el.querySelector<HTMLElement>('[class$="-wrapper"]');
      // 页面元素的类名 = 我们传入的 className（这里是 'docx-preview'，即 section.docx-preview），
      // 不是固定的 .docx。直接取 wrapper 下的所有 section 最稳，不依赖具体类名。
      const pages = wrapper?.querySelectorAll<HTMLElement>(':scope > section');
      if (!wrapper || !pages || pages.length === 0) return;
      // 清掉 wrapper 默认的灰背景和内边距，避免缩放后露出灰条；改为纵向堆叠页面并居中。
      wrapper.style.background = 'transparent';
      wrapper.style.padding = '0';
      wrapper.style.display = 'flex';
      wrapper.style.flexDirection = 'column';
      wrapper.style.alignItems = 'center';
      wrapper.style.gap = '12px';

      // 先还原再测量，否则会拿到已缩放后的尺寸，多次 resize 后越测越小。
      pages.forEach((p) => {
        p.style.transform = '';
        p.style.margin = '0';
      });
      const first = pages[0];
      const contentWidth = first.getBoundingClientRect().width;
      if (!contentWidth) return;
      const avail = el.clientWidth - 24; // 减去容器左右 padding（px-3 = 24px）
      const scale = Math.min(1, avail / contentWidth); // 只缩小不放大

      pages.forEach((p) => {
        const h = p.getBoundingClientRect().height;
        p.style.transformOrigin = 'top center';
        p.style.transform = `scale(${scale})`;
        // transform 不改变占位尺寸：缩小后占位仍是原始宽高，会撑出横向滚动条、底部多留白。
        // 用负 margin 收掉两侧和底部多出的占位（top center 缩放，左右各超出半份差值）。
        const dx = contentWidth * (1 - scale);
        p.style.marginLeft = `${-dx / 2}px`;
        p.style.marginRight = `${-dx / 2}px`;
        p.style.marginBottom = `${-h * (1 - scale)}px`;
      });
    };

    renderAsync(blob, el, undefined, {
      className: 'docx-preview',
      inWrapper: true,
      ignoreWidth: false,
      ignoreHeight: false,
    })
      .then(() => {
        if (cancelled) return;
        setStatus('done');
        // setStatus 是异步的，此刻容器仍可能是 display:none（getBoundingClientRect 全为 0）。
        // 等两帧让 React 把 display 切回、浏览器完成 layout 后再测量缩放。
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            if (!cancelled) applyScale();
          }),
        );
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : '渲染失败');
        setStatus('error');
      });

    // 拖动预览窗口时实时重算缩放，跟随容器宽度。
    const ro = new ResizeObserver(() => applyScale());
    ro.observe(el);

    return () => {
      cancelled = true;
      ro.disconnect();
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
        className="cw-scroll overflow-auto bg-paper-2 py-3 px-3"
        style={{
          display: status === 'done' ? undefined : 'none',
          maxHeight: 'calc(100vh - 160px)',
        }}
      />
    </div>
  );
}
