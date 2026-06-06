import { useEffect, useState } from 'react';
import * as XLSX from 'xlsx';

type Sheet = { name: string; rows: string[][] };

export default function ExcelPreview({ blob }: { blob: Blob }) {
  const [sheets, setSheets] = useState<Sheet[] | null>(null);
  const [active, setActive] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSheets(null);
    setError(null);
    setActive(0);

    blob
      .arrayBuffer()
      .then((buf) => {
        if (cancelled) return;
        const wb = XLSX.read(buf, { type: 'array' });
        const parsed: Sheet[] = wb.SheetNames.map((name) => {
          const ws = wb.Sheets[name];
          const rows = XLSX.utils.sheet_to_json<string[]>(ws, {
            header: 1,
            blankrows: false,
            defval: '',
            raw: false,
          });
          return { name, rows };
        });
        setSheets(parsed);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : '解析失败');
      });

    return () => {
      cancelled = true;
    };
  }, [blob]);

  if (error) {
    return <div className="font-mono text-[11px] text-accent">表格解析失败：{error}</div>;
  }
  if (!sheets) {
    return <div className="font-mono text-[11px] text-dim">解析表格…</div>;
  }

  const sheet = sheets[active];

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="cw-label px-4 py-2 border-b border-line-2 flex items-center gap-2 flex-wrap">
        表格预览
        {sheets.length > 1 &&
          sheets.map((s, i) => (
            <button
              key={s.name}
              onClick={() => setActive(i)}
              className={`font-mono text-[10.5px] px-1.5 py-0.5 rounded ${
                i === active ? 'bg-accent-soft text-accent' : 'text-dim hover:bg-paper-2'
              }`}
            >
              {s.name}
            </button>
          ))}
      </div>
      <div className="cw-scroll overflow-auto max-h-[560px]">
        {sheet.rows.length === 0 ? (
          <div className="px-4 py-3 font-mono text-[11px] text-dim italic">（空工作表）</div>
        ) : (
          <table className="border-collapse font-mono text-[11.5px] text-ink-2">
            <tbody>
              {sheet.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td
                      key={c}
                      className="border border-line-2 px-2 py-1 whitespace-nowrap align-top"
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
