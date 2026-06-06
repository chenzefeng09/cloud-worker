import { useState } from 'react';
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

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="cw-label px-4 py-2 border-b border-line-2 flex items-center gap-2">
        PDF 预览
        {numPages != null && (
          <span className="font-mono text-[10px] text-dim">共 {numPages} 页</span>
        )}
      </div>
      <div className="cw-scroll px-3 py-3 max-h-[640px] overflow-auto flex flex-col items-center gap-3 bg-paper-2">
        {error ? (
          <div className="font-mono text-[11px] text-accent py-4">{error}</div>
        ) : (
          <Document
            file={url}
            options={DOC_OPTIONS}
            onLoadSuccess={({ numPages: n }) => setNumPages(n)}
            onLoadError={(e) => setError(`PDF 加载失败：${e.message}`)}
            loading={<div className="font-mono text-[11px] text-dim py-4">加载 PDF…</div>}
            error={<div className="font-mono text-[11px] text-accent py-4">无法渲染该 PDF</div>}
          >
            {Array.from({ length: numPages ?? 0 }, (_, i) => (
              <Page
                key={i}
                pageNumber={i + 1}
                width={680}
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
