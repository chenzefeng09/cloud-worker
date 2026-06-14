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
