import { useEffect, useRef, useState } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';

// pdf.js worker：用 Vite 的 import.meta.url 机制让 worker 被打成独立资源，
// 整个 react-pdf + pdfjs 只在本组件被 lazy 加载时才进入网络请求。
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString();

const DOC_OPTIONS = { withCredentials: true } as const;

export default function PdfPreview({ url }: { url: string }) {
  const [numPages, setNumPages] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 让 PDF 页面宽度跟随滚动容器实际内容宽度，拖动预览窗口时自适应、不出横向滚动。
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pageWidth, setPageWidth] = useState<number | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => {
      // clientWidth 已扣掉滚动条，再减去左右 padding（px-3 = 24px）让页面正好贴合内容区。
      const w = el.clientWidth - 24;
      setPageWidth(w > 200 ? w : 200);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="cw-label px-4 py-2 border-b border-line-2 flex items-center gap-2">
        PDF preview
        {numPages != null && (
          <span className="font-mono text-[10px] text-dim">{numPages} {numPages === 1 ? 'page' : 'pages'}</span>
        )}
      </div>
      <div ref={scrollRef} className="cw-scroll px-3 py-3 max-h-[640px] overflow-auto flex flex-col items-center gap-3 bg-paper-2">
        {error ? (
          <div className="font-mono text-[11px] text-accent py-4">{error}</div>
        ) : (
          <Document
            file={url}
            options={DOC_OPTIONS}
            onLoadSuccess={({ numPages: n }) => setNumPages(n)}
            onLoadError={(e) => setError(`Failed to load PDF: ${e.message}`)}
            loading={<div className="font-mono text-[11px] text-dim py-4">Loading PDF…</div>}
            error={<div className="font-mono text-[11px] text-accent py-4">Unable to render this PDF</div>}
          >
            {pageWidth != null &&
              Array.from({ length: numPages ?? 0 }, (_, i) => (
                <Page
                  key={i}
                  pageNumber={i + 1}
                  width={pageWidth}
                  className="shadow-sm mb-2"
                  renderAnnotationLayer={false}
                  renderTextLayer
                />
              ))}
          </Document>
        )}
      </div>
    </div>
  );
}
