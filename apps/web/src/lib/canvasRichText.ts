import type { CanvasBlock, CanvasSelectionSegment } from '../api/types';
import type { CanvasTextSelection } from './canvasObjects';

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function inlineMarkdownToHtml(value: string): string {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function isTableSeparator(line: string | undefined): boolean {
  if (!line) return false;
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
}

function isBlockBoundary(line: string, nextLine?: string): boolean {
  return (
    line.trim() === '' ||
    /^(#{1,3})\s+/.test(line) ||
    /^-{3,}$/.test(line.trim()) ||
    /^>\s?/.test(line) ||
    /^[-*]\s+/.test(line) ||
    /^\d+\.\s+/.test(line) ||
    isTableSeparator(nextLine)
  );
}

export function markdownToCanvasHtml(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const html: string[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    if (line.includes('|') && isTableSeparator(lines[i + 1])) {
      const head = splitTableRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      html.push(
        `<table><thead><tr>${head
          .map((cell) => `<th>${inlineMarkdownToHtml(cell)}</th>`)
          .join('')}</tr></thead><tbody>${rows
          .map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdownToHtml(cell)}</td>`).join('')}</tr>`)
          .join('')}</tbody></table>`,
      );
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      html.push(`<h${heading[1].length}>${inlineMarkdownToHtml(heading[2])}</h${heading[1].length}>`);
      i += 1;
      continue;
    }

    if (/^-{3,}$/.test(trimmed)) {
      html.push('<hr>');
      i += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const parts: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        parts.push(lines[i].replace(/^>\s?/, ''));
        i += 1;
      }
      html.push(`<blockquote>${inlineMarkdownToHtml(parts.join('\n')).replace(/\n/g, '<br>')}</blockquote>`);
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ''));
        i += 1;
      }
      html.push(`<ul>${items.map((item) => `<li>${inlineMarkdownToHtml(item)}</li>`).join('')}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ''));
        i += 1;
      }
      html.push(`<ol>${items.map((item) => `<li>${inlineMarkdownToHtml(item)}</li>`).join('')}</ol>`);
      continue;
    }

    const paragraph: string[] = [];
    while (i < lines.length && !isBlockBoundary(lines[i], lines[i + 1])) {
      paragraph.push(lines[i].trim());
      i += 1;
    }
    html.push(`<p>${inlineMarkdownToHtml(paragraph.join(' '))}</p>`);
  }

  return html.length > 0 ? html.join('') : '<p><br></p>';
}

function escapeAttribute(value: string): string {
  return escapeHtml(value).replace(/'/g, '&#39;');
}

/**
 * Render server-owned blocks without throwing their stable ids away.  The
 * wrapper is deliberately part of the editable DOM: selection requests and
 * manual saves can now address the original block even after other blocks are
 * inserted or deleted.
 */
export function canvasBlocksToHtml(blocks: CanvasBlock[]): string {
  if (blocks.length === 0) return '<div data-canvas-block-id=""><p><br></p></div>';
  return blocks
    .map(
      (block) =>
        `<div data-canvas-block-id="${escapeAttribute(block.id)}" data-canvas-block-type="${block.type}">` +
        `${markdownToCanvasHtml(block.text)}</div>`,
    )
    .join('');
}

function nodeChildrenToMarkdown(node: Node): string {
  return Array.from(node.childNodes).map((child) => nodeToMarkdown(child)).join('');
}

function nodeToMarkdown(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
  if (!(node instanceof HTMLElement)) return '';

  const content = nodeChildrenToMarkdown(node);
  switch (node.tagName.toLowerCase()) {
    case 'strong':
    case 'b':
      return `**${content}**`;
    case 'em':
    case 'i':
      return `*${content}*`;
    case 'code':
      return `\`${content}\``;
    case 'br':
      return '\n';
    case 'a': {
      const href = node.getAttribute('href');
      return href ? `[${content}](${href})` : content;
    }
    default:
      return content;
  }
}

function blockToMarkdown(node: ChildNode): string {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent ?? '').trim();
  if (!(node instanceof HTMLElement)) return '';

  const tag = node.tagName.toLowerCase();
  if (node.hasAttribute('data-canvas-block-id')) {
    return canvasHtmlToMarkdown(node);
  }
  const content = nodeChildrenToMarkdown(node).trim();
  if (!content && tag !== 'hr') return '';

  if (tag === 'h1') return `# ${content}`;
  if (tag === 'h2') return `## ${content}`;
  if (tag === 'h3') return `### ${content}`;
  if (tag === 'blockquote') {
    return content
      .split('\n')
      .map((line) => `> ${line}`)
      .join('\n');
  }
  if (tag === 'ul') {
    return Array.from(node.children)
      .filter((child) => child.tagName.toLowerCase() === 'li')
      .map((child) => `- ${nodeChildrenToMarkdown(child).trim()}`)
      .join('\n');
  }
  if (tag === 'ol') {
    return Array.from(node.children)
      .filter((child) => child.tagName.toLowerCase() === 'li')
      .map((child, index) => `${index + 1}. ${nodeChildrenToMarkdown(child).trim()}`)
      .join('\n');
  }
  if (tag === 'hr') return '---';
  if (tag === 'table') {
    const rows = Array.from(node.querySelectorAll('tr')).map((row) =>
      Array.from(row.children).map((cell) => nodeChildrenToMarkdown(cell).replace(/\s+/g, ' ').trim()),
    );
    if (rows.length === 0) return '';
    const [head, ...body] = rows;
    return [
      `| ${head.join(' | ')} |`,
      `| ${head.map(() => '---').join(' | ')} |`,
      ...body.map((row) => `| ${row.join(' | ')} |`),
    ].join('\n');
  }
  if (tag === 'pre') return `\`\`\`\n${node.textContent?.trim() ?? ''}\n\`\`\``;
  return content;
}

export function canvasHtmlToMarkdown(root: HTMLElement): string {
  return Array.from(root.childNodes)
    .map((node) => blockToMarkdown(node))
    .filter(Boolean)
    .join('\n\n')
    .trim();
}

/** Read a contentEditable canvas back into blocks while preserving DOM ids. */
export function canvasEditorToBlocks(
  root: HTMLElement,
  previousBlocks: CanvasBlock[],
  makeBlockId: () => string,
): CanvasBlock[] {
  const previousById = new Map(previousBlocks.map((block) => [block.id, block]));
  const used = new Set<string>();
  const result: CanvasBlock[] = [];

  for (const child of Array.from(root.childNodes)) {
    const text = child instanceof HTMLElement && child.hasAttribute('data-canvas-block-id')
      ? canvasHtmlToMarkdown(child)
      : blockToMarkdown(child);
    if (!text.trim()) continue;

    const domId = child instanceof HTMLElement ? child.dataset.canvasBlockId : undefined;
    const id = domId && previousById.has(domId) && !used.has(domId) ? domId : makeBlockId();
    used.add(id);
    const previous = previousById.get(id);
    const type: CanvasBlock['type'] = /^#{1,6}\s/.test(text)
      ? 'heading'
      : previous?.type ?? 'paragraph';
    result.push({ id, type, text });
  }

  return result;
}

function markdownToPlainText(markdown: string): string {
  const host = document.createElement('div');
  host.innerHTML = markdownToCanvasHtml(markdown);
  return Array.from(host.childNodes)
    .map((node) => node.textContent?.trim() ?? '')
    .filter(Boolean)
    .join('\n');
}

export function findMarkdownSelection(
  markdown: string,
  selectedText: string,
  renderedStart: number,
): CanvasTextSelection {
  const text = selectedText.replace(/\u00a0/g, ' ');
  const matches: number[] = [];
  let index = markdown.indexOf(text);
  while (index !== -1) {
    matches.push(index);
    index = markdown.indexOf(text, index + Math.max(1, text.length));
  }

  if (matches.length === 1) {
    return { text, start: matches[0], end: matches[0] + text.length };
  }

  if (matches.length > 1) {
    let best = matches[0];
    let bestDistance = Number.POSITIVE_INFINITY;
    for (const match of matches) {
      const plainBefore = markdownToPlainText(markdown.slice(0, match)).length;
      const distance = Math.abs(plainBefore - renderedStart);
      if (distance < bestDistance) {
        best = match;
        bestDistance = distance;
      }
    }
    return { text, start: best, end: best + text.length };
  }

  return { text, start: renderedStart, end: renderedStart + text.length };
}

export function getMarkdownSelectionInside(
  root: HTMLElement,
  markdown: string,
): CanvasTextSelection | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  if (range.collapsed) return null;
  if (!root.contains(range.commonAncestorContainer)) return null;

  const before = range.cloneRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);

  const text = range.toString();
  if (!text.trim()) return null;

  const start = before.toString().length;
  return findMarkdownSelection(markdown, text, start);
}

/**
 * Resolve the browser Range into one exact, block-local segment per touched
 * server block.  Each segment carries the canonical markdown slice, not just
 * rendered text, so the BFF can verify it against the current version before
 * spending a model call.
 */
export function getCanvasBlockSelectionsInside(
  root: HTMLElement,
  blocks: CanvasBlock[],
): CanvasSelectionSegment[] {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return [];
  const range = selection.getRangeAt(0);
  if (range.collapsed || !root.contains(range.commonAncestorContainer)) return [];

  const byId = new Map(blocks.map((block) => [block.id, block]));
  const segments: CanvasSelectionSegment[] = [];

  for (const node of Array.from(root.querySelectorAll<HTMLElement>(':scope > [data-canvas-block-id]'))) {
    const blockId = node.dataset.canvasBlockId;
    const block = blockId ? byId.get(blockId) : undefined;
    if (!block || !range.intersectsNode(node)) continue;

    const intersection = range.cloneRange();
    if (!node.contains(intersection.startContainer)) {
      intersection.setStart(node, 0);
    }
    if (!node.contains(intersection.endContainer)) {
      intersection.setEnd(node, node.childNodes.length);
    }
    const renderedText = intersection.toString().replace(/\u00a0/g, ' ');
    if (!renderedText.trim()) continue;

    const before = document.createRange();
    before.selectNodeContents(node);
    before.setEnd(intersection.startContainer, intersection.startOffset);
    const local = findMarkdownSelection(block.text, renderedText, before.toString().length);
    const canonicalText = block.text.slice(local.start, local.end);
    if (!canonicalText) continue;
    segments.push({
      block_id: block.id,
      start: local.start,
      end: local.end,
      text: canonicalText,
    });
  }

  return segments;
}
