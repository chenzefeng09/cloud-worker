/**
 * Shared remark/rehype plugin sets for every place we render markdown.
 *
 * Chat replies, the file-preview panel and app results all render the same
 * agent-authored markdown, so they must agree on what is supported — a report
 * whose formulas render in the chat but show as raw `$$\frac{}{}$$` in the
 * preview panel is the kind of drift this module exists to prevent.
 */

import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';

export const markdownRemarkPlugins = [remarkGfm, remarkMath];

/**
 * Rewrite LaTeX's own `\[…\]` / `\(…\)` delimiters to the `$$…$$` / `$…$` that
 * remark-math understands.
 *
 * Models emit both conventions for the same report — one MATLAB run wrote
 * `$$ G(s) = … $$`, the next wrote `\[ G(s) = … \]` — and the second kind would
 * otherwise reach the reader as raw backslashes. Code spans and fenced blocks
 * are left untouched: a shell snippet containing `\(` is not math.
 */
export function normalizeMathDelimiters(src: string): string {
  // Odd indices are the captured code segments; only rewrite the even ones.
  return src
    .split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)/g)
    .map((seg, i) =>
      i % 2 === 1
        ? seg
        : seg
            .replace(/\\\[([\s\S]*?)\\\]/g, (_m, body: string) => `$$${body}$$`)
            .replace(/\\\(([\s\S]*?)\\\)/g, (_m, body: string) => `$${body}$`),
    )
    .join('');
}

// `throwOnError: false` matters while streaming: a half-typed `$$` arrives as
// invalid TeX for a few frames, and we want the raw text shown rather than an
// exception taking the whole message down.
export const markdownRehypePlugins = [
  [rehypeHighlight, { detect: true, ignoreMissing: true }],
  [rehypeKatex, { throwOnError: false, output: 'html' }],
] as const;
