import { useState } from 'react';

interface CanvasHtmlPreviewProps {
  /** Complete self-contained HTML document (stored verbatim server-side). */
  html: string;
  /** Canvas title — used as the iframe accessible title. */
  title: string;
  /** Bump to force the iframe to remount (e.g. the canvas' current_version). */
  reloadKey?: string | number;
}

/**
 * Runs an executable canvas (content_type="html") in an isolated sandbox iframe,
 * with a 预览 / 代码 toggle.
 *
 * Isolation contract — identical to filePreview's HtmlPreview:
 * `sandbox="allow-scripts"` WITHOUT `allow-same-origin`. With `srcDoc` this puts
 * the frame in a null (opaque) origin, so the AI-generated scripts can run (a
 * playable game, an animation) but cannot read the parent app's
 * cookies / localStorage / same-origin data. NEVER add `allow-same-origin`: the
 * combination would let untrusted scripts touch same-origin state → XSS. The
 * HTML is only ever handed to `srcDoc`, never injected into the parent DOM.
 */
export function CanvasHtmlPreview({ html, title, reloadKey }: CanvasHtmlPreviewProps) {
  const [mode, setMode] = useState<'preview' | 'code'>('preview');

  if (!html.trim()) {
    return <div className="px-4 py-6 font-mono text-[11px] text-dim">生成中…</div>;
  }

  const seg = (active: boolean) =>
    `px-2.5 h-7 inline-flex items-center rounded-md border border-line text-[12px] font-mono transition-colors ${
      active ? 'text-ink bg-paper-2' : 'text-dim hover:text-ink hover:border-ink-2'
    }`;

  return (
    <div className="flex flex-col gap-2 p-3">
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          className={seg(mode === 'preview')}
          style={mode === 'preview' ? { borderColor: 'var(--accent)' } : undefined}
          onClick={() => setMode('preview')}
        >
          预览
        </button>
        <button
          type="button"
          className={seg(mode === 'code')}
          style={mode === 'code' ? { borderColor: 'var(--accent)' } : undefined}
          onClick={() => setMode('code')}
        >
          代码
        </button>
      </div>

      {mode === 'preview' ? (
        <div className="border border-line rounded-md overflow-hidden bg-paper-2">
          <iframe
            key={reloadKey}
            sandbox="allow-scripts"
            srcDoc={html}
            title={title}
            // Runnable apps often center themselves in a 100vh body, so a short
            // fixed height clips the HUD top/bottom. Track the viewport (capped)
            // so games fit; min keeps small widgets from collapsing.
            style={{ width: '100%', height: 'min(74vh, 760px)', minHeight: 480, border: 0, background: '#fff' }}
            className="min-h-[480px]"
          />
        </div>
      ) : (
        <pre className="cw-scroll max-h-[520px] overflow-auto rounded-md border border-line bg-paper-2 p-3 text-[11.5px] leading-relaxed">
          <code className="font-mono whitespace-pre">{html}</code>
        </pre>
      )}

      <div className="font-mono text-[10.5px] text-dim">
        在隔离沙箱中运行，无法访问站点数据。内容需为完整自包含 HTML（内联脚本/样式，勿引用外部资源）。
      </div>
    </div>
  );
}
