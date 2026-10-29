export type CanvasObjectKind =
  | 'article'
  | 'brief'
  | 'email'
  | 'report'
  | 'proposal'
  | 'social_post'
  | 'code_note';

export type CanvasEditScope = 'selection' | 'document';

export interface CanvasObjectDefinition {
  kind: CanvasObjectKind;
  label: string;
  description: string;
  defaultTitle: string;
  starter: string;
  editIntents: CanvasEditIntent[];
}

export interface CanvasEditIntent {
  id: string;
  label: string;
  instruction: string;
}

export interface CanvasDocumentDraft {
  id: string;
  kind: CanvasObjectKind;
  title: string;
  content: string;
  sourceMessageId?: string;
  updatedAt: string;
}

export interface CanvasOpenSeed {
  kind?: CanvasObjectKind;
  title?: string;
  content: string;
  sourceMessageId?: string;
}

export interface CanvasExtractedDocument {
  kind: CanvasObjectKind;
  title: string;
  content: string;
  sourceMessageId?: string;
}

export interface CanvasTextSelection {
  text: string;
  start: number;
  end: number;
}

export interface CanvasEditRequest {
  documentId: string;
  kind: CanvasObjectKind;
  title: string;
  scope: CanvasEditScope;
  instruction: string;
  content: string;
  selection?: CanvasTextSelection;
}

export interface CanvasEditSuggestion {
  request: CanvasEditRequest;
  replacement: string;
  summary?: string | null;
  receivedAt: string;
}

export interface CanvasPatchOperation {
  op: 'replace_text' | 'insert_text' | 'delete_text' | 'replace_document';
  start?: number;
  end?: number;
  text?: string;
}

export interface CanvasPatchEnvelope {
  documentId: string;
  baseRevisionId?: string;
  operations: CanvasPatchOperation[];
  summary?: string;
}

export interface CanvasPersistenceAdapter {
  load(sessionId: string, documentId: string): Promise<CanvasDocumentDraft | null>;
  save(sessionId: string, draft: CanvasDocumentDraft): Promise<CanvasDocumentDraft>;
  requestEdit(request: CanvasEditRequest): Promise<CanvasPatchEnvelope | null>;
  applyPatch(sessionId: string, patch: CanvasPatchEnvelope): Promise<CanvasDocumentDraft>;
}

export const CANVAS_OBJECTS: CanvasObjectDefinition[] = [
  {
    kind: 'article',
    label: 'Article',
    description: 'Blog posts, newsletters, long-form pieces',
    defaultTitle: 'Untitled article',
    starter:
      'Title\n\nOpening: state the problem this article solves in a sentence or two.\n\n1. Key point\n\n2. Analysis\n\n3. Recommendations\n\nClosing: give the reader a clear takeaway.',
    editIntents: [
      { id: 'polish', label: 'Polish', instruction: 'Keep the meaning but make the writing clearer, more natural, and better paced.' },
      { id: 'expand', label: 'Expand', instruction: 'Add supporting points and transitions to make the piece more complete.' },
      { id: 'shorten', label: 'Shorten', instruction: 'Cut redundant phrasing and keep the key information.' },
    ],
  },
  {
    kind: 'brief',
    label: 'Brief',
    description: 'Meeting notes, project updates, decision summaries',
    defaultTitle: 'Work brief',
    starter:
      'Background\n\nKey progress\n\nRisks and blockers\n\nNext steps\n\nDecisions needed',
    editIntents: [
      { id: 'executive', label: 'Executive summary', instruction: 'Turn this into a skimmable executive summary that highlights conclusions, risks, and actions.' },
      { id: 'actionable', label: 'Make actionable', instruction: 'Rewrite the content as clear owners, actions, and deadlines.' },
    ],
  },
  {
    kind: 'email',
    label: 'Email',
    description: 'Customers, suppliers, internal communication',
    defaultTitle: 'Email draft',
    starter:
      'Subject:\n\nHi,\n\nBody\n\nThanks,\n',
    editIntents: [
      { id: 'warmer', label: 'Warmer', instruction: 'Make the tone friendlier and professional without being wordy.' },
      { id: 'firmer', label: 'Firmer', instruction: 'Make the tone clearer and more assertive while staying polite.' },
    ],
  },
  {
    kind: 'report',
    label: 'Report',
    description: 'Research, retrospectives, analysis reports',
    defaultTitle: 'Analysis report',
    starter:
      'Summary\n\nBackground\n\nFindings\n\nAnalysis\n\nRecommendations\n\nAppendix',
    editIntents: [
      { id: 'structured', label: 'Restructure', instruction: 'Reorganize so the heading hierarchy, line of argument, and conclusions are clearer.' },
      { id: 'evidence', label: 'Add evidence', instruction: 'Point out where data, examples, or citations are needed and draft wording to fill them in.' },
    ],
  },
  {
    kind: 'proposal',
    label: 'Proposal',
    description: 'Product, project, and business proposals',
    defaultTitle: 'Proposal draft',
    starter:
      'Goals\n\nCurrent state and problems\n\nProposed design\n\nImplementation plan\n\nCosts and risks\n\nAcceptance criteria',
    editIntents: [
      { id: 'sharpen', label: 'More persuasive', instruction: 'Strengthen the value proposition, the reasoning behind trade-offs, and the rollout path.' },
      { id: 'risks', label: 'Add risks', instruction: 'Add risks, constraints, prerequisites, and fallback plans.' },
    ],
  },
  {
    kind: 'social_post',
    label: 'Social post',
    description: 'Social media posts and short updates',
    defaultTitle: 'Social post draft',
    starter:
      'Hook:\n\nBody:\n\nEngagement question:',
    editIntents: [
      { id: 'hook', label: 'Stronger hook', instruction: 'Rewrite the opening so it grabs attention in the first few seconds, without exaggerating.' },
      { id: 'punchy', label: 'Shorter and punchier', instruction: 'Make it shorter, more conversational, and more memorable.' },
    ],
  },
  {
    kind: 'code_note',
    label: 'Tech note',
    description: 'READMEs, design docs, API docs',
    defaultTitle: 'Tech note',
    starter:
      'Background\n\nGoals\n\nAPIs / modules\n\nKey flows\n\nEdge cases\n\nTesting and verification',
    editIntents: [
      { id: 'precise', label: 'More precise', instruction: 'Fix vague wording so technical boundaries, inputs/outputs, and failure cases are explicit.' },
      { id: 'readme', label: 'README style', instruction: 'Rewrite in a README style that developers can pick up easily.' },
    ],
  },
];

export const DEFAULT_CANVAS_OBJECT = CANVAS_OBJECTS[0];

export function getCanvasObject(kind: CanvasObjectKind): CanvasObjectDefinition {
  return CANVAS_OBJECTS.find((item) => item.kind === kind) ?? DEFAULT_CANVAS_OBJECT;
}

export function createCanvasDraft(
  kind: CanvasObjectKind,
  seed?: Partial<Pick<CanvasDocumentDraft, 'title' | 'content' | 'sourceMessageId'>>,
): CanvasDocumentDraft {
  const object = getCanvasObject(kind);
  return {
    id: `canvas_${Date.now()}`,
    kind,
    title: seed?.title?.trim() || object.defaultTitle,
    content: seed?.content ?? object.starter,
    sourceMessageId: seed?.sourceMessageId,
    updatedAt: new Date().toISOString(),
  };
}

export function extractCanvasReplacement(content: string): string | null {
  const match = content.match(/<canvas-replacement>\s*([\s\S]*?)\s*<\/canvas-replacement>/i);
  if (!match) return null;
  const replacement = match[1].trim();
  const fenced = replacement.match(/^```(?:\w+)?\n([\s\S]*?)\n```$/);
  return fenced ? fenced[1].trim() : replacement;
}

function stripOuterCodeFence(content: string): string {
  const fenced = content.trim().match(/^```(?:markdown|md|text)?\s*\n([\s\S]*?)\n```$/i);
  return fenced ? fenced[1].trim() : content.trim();
}

function isHorizontalRule(line: string): boolean {
  return /^(?:-{3,}|\*{3,}|_{3,})$/.test(line.trim());
}

function normalizeTitle(value: string): string {
  return value
    .replace(/^#{1,6}\s+/, '')
    .replace(/^(?:标题|title)\s*[:：]\s*/i, '')
    .replace(/[*_`~]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function normalizeMetaText(value: string): string {
  return normalizeTitle(value)
    .replace(/<[^>]+>/g, '')
    .replace(/^[-*•]\s+/, '')
    .replace(/\s+([:：])/g, '$1')
    .trim();
}

function isGenericCanvasTitle(title: string | undefined, kind: CanvasObjectKind): boolean {
  const object = getCanvasObject(kind);
  const normalized = normalizeTitle(title ?? '').replace(/\s+/g, '').toLowerCase();
  if (!normalized) return true;
  return [
    object.defaultTitle.replace(/\s+/g, '').toLowerCase(),
    'ai回答草稿',
    'ai草稿',
    '回答草稿',
    '文章草稿',
    '草稿',
    'draft',
    'aidraft',
    'untitled',
  ].includes(normalized);
}

function hasAssistantPrelude(content: string): boolean {
  return /(?:需要先确认|才能让稿件|我来写|下面是|以下是|为你|给你|请问|(?:草稿|正文|版本)(?:如下|[:：]))/.test(
    content,
  );
}

function stripLeadingSeparatorPrelude(content: string): string {
  const lines = content.split('\n');
  const separatorIndex = lines.findIndex(isHorizontalRule);
  if (separatorIndex <= 0) return content.trim();

  const before = lines.slice(0, separatorIndex).join('\n').trim();
  const after = lines.slice(separatorIndex + 1).join('\n').trim();
  if (!after || !hasAssistantPrelude(before)) return content.trim();
  return after;
}

function isLeadingMetaLine(line: string): boolean {
  const normalized = normalizeTitle(line);
  if (!normalized) return false;
  if (/^(?:▶\s*)?(?:thought|思考|分析)\b/i.test(normalized)) return true;
  if (/^(?:ask_user|tool|function)\b/i.test(normalized)) return true;
  if (/^(?:写|撰写|起草|生成|创作).{0,80}(?:需要|建议|先确认|确认|先明确).{0,80}(?:信息|场景|主题|对象|方向|要求)/.test(normalized)) {
    return true;
  }
  if (/^(?:为了|为让).{0,40}(?:更贴合|更准确|更符合).{0,40}(?:需要|建议|先)/.test(normalized)) {
    return true;
  }
  if (/^请问.{0,80}(?:吗|？|\?)/.test(normalized)) return true;
  if (/^当然可以[，,。\s]*(?:我来|下面|以下|为你|给你|这就|先按|这里|将|面向)/.test(normalized)) {
    return true;
  }
  if (/^(?:好的|当然|可以|没问题|收到|明白)[，,。\s]*(?:我来|下面|以下|为你|给你|这就|先按|这里|将|面向)/.test(normalized)) {
    return true;
  }
  if (/^(?:下面|以下|这里|这是|为你|给你).{0,30}(?:文章|稿件|讲话|草稿|正文|版本|内容|邮件|报告|方案)/.test(normalized)) {
    return true;
  }
  if (/^我(?:先|会|来).{0,40}(?:写|生成|整理|起草).{0,40}(?:如下|下面|版本|稿)/.test(normalized)) {
    return true;
  }
  if (/^如果.{0,30}(?:没有|未指定).{0,60}(?:我先|默认|先按)/.test(normalized)) {
    return true;
  }
  // English assistant preludes ("Sure, here's a draft…", "Here is the email:").
  if (/^(?:sure|of course|certainly|okay|ok|got it)[,.!\s]+(?:here(?:'s|’s| is| are)|i(?:'ll|’ll| will))\b/i.test(normalized)) {
    return true;
  }
  if (/^here(?:'s|’s| is| are)\b.{0,40}\b(?:draft|article|email|report|proposal|version|post|brief)\b.{0,40}[:：]$/i.test(normalized)) {
    return true;
  }
  return false;
}

function stripLeadingAssistantMeta(content: string): string {
  const lines = content.split('\n');
  let start = 0;
  let stripped = false;

  while (start < lines.length) {
    while (start < lines.length && !lines[start].trim()) start += 1;
    if (start < lines.length && isHorizontalRule(lines[start])) {
      start += 1;
      stripped = true;
      continue;
    }
    if (start < lines.length && isLeadingMetaLine(lines[start])) {
      start += 1;
      stripped = true;
      continue;
    }
    break;
  }

  return stripped ? lines.slice(start).join('\n').trim() : content.trim();
}

function splitParagraphs(content: string): string[] {
  return content
    .trim()
    .split(/\n{2,}/)
    .map((chunk) => chunk.trim())
    .filter(Boolean);
}

const TRAILING_META_LABELS = [
  '时长说明',
  '说明',
  '备注',
  '注',
  '篇幅说明',
  '使用建议',
  '修改建议',
  '可选调整',
  '温馨提示',
  'Note',
  'Notes',
];

function hasTrailingMetaLabel(value: string): boolean {
  const compact = value.replace(/\s+/g, '');
  return TRAILING_META_LABELS.some((label) =>
    compact.startsWith(`${label}：`) || compact.startsWith(`${label}:`),
  );
}

function isTrailingMetaChunk(chunk: string): boolean {
  const normalized = normalizeMetaText(
    chunk
      .split('\n')
      .filter((line) => !isHorizontalRule(line))
      .join('\n'),
  );
  if (!normalized) return true;
  if (hasTrailingMetaLabel(normalized)) return true;
  if (/^(?:如果|如需|若需要).{0,90}(?:调整|修改|改成|告诉我|继续|可以再)/.test(normalized)) {
    return true;
  }
  if (/^(?:以上|这篇|这份).{0,20}(?:稿件|内容|文章|讲话稿).{0,100}(?:适合|可以|约|按|已经|朗读)/.test(normalized)) {
    return true;
  }
  return false;
}

function stripTrailingAssistantMeta(content: string): string {
  const lineCleaned = stripTrailingAssistantMetaLines(content);
  const paragraphs = splitParagraphs(lineCleaned);
  while (paragraphs.length > 1 && isTrailingMetaChunk(paragraphs[paragraphs.length - 1])) {
    paragraphs.pop();
  }
  return paragraphs.join('\n\n').trim();
}

function stripTrailingAssistantMetaLines(content: string): string {
  const lines = content.trim().split('\n');
  let end = lines.length;
  while (end > 0 && !lines[end - 1].trim()) end -= 1;

  let metaStart = -1;
  for (let index = end - 1; index >= 0; index -= 1) {
    const line = lines[index];
    if (!line.trim()) {
      if (metaStart !== -1) metaStart = index;
      continue;
    }

    if (isTrailingMetaChunk(lines.slice(index, end).join('\n'))) {
      metaStart = index;
      continue;
    }

    if (metaStart !== -1 && isHorizontalRule(line)) {
      metaStart = index;
      continue;
    }

    break;
  }

  if (metaStart === -1) return content.trim();

  while (metaStart > 0 && !lines[metaStart - 1].trim()) metaStart -= 1;
  if (metaStart > 0 && isHorizontalRule(lines[metaStart - 1])) {
    metaStart -= 1;
  }
  while (metaStart > 0 && !lines[metaStart - 1].trim()) metaStart -= 1;

  return lines.slice(0, metaStart).join('\n').trim();
}

function titleCandidateFromLine(line: string): string | null {
  const trimmed = line.trim();
  const heading = trimmed.match(/^#{1,3}\s+(.+)$/);
  if (heading) return normalizeTitle(heading[1]);

  const labelled = trimmed.match(/^(?:标题|title)\s*[:：]\s*(.+)$/i);
  if (labelled) return normalizeTitle(labelled[1]);

  const normalized = normalizeTitle(trimmed);
  if (!normalized || normalized.length > 52) return null;
  if (/[。；;]$/.test(normalized)) return null;
  if (/^(?:尊敬的|亲爱的|各位|大家|今天|我们|首先|第一|一、|二、|三、|四、|五、|1\.|[-*•])/i.test(normalized)) {
    return null;
  }
  if (isLeadingMetaLine(normalized)) return null;
  return normalized;
}

function extractTitleAndBody(content: string): { title: string | null; body: string } {
  const lines = content.split('\n');
  const firstContentIndex = lines.findIndex((line) => line.trim());
  if (firstContentIndex === -1) return { title: null, body: '' };

  const title = titleCandidateFromLine(lines[firstContentIndex]);
  if (!title) return { title: null, body: content.trim() };

  const nextLines = lines.slice();
  nextLines.splice(firstContentIndex, 1);
  while (nextLines.length > 0 && !nextLines[0].trim()) nextLines.shift();
  if (nextLines.length > 0 && isHorizontalRule(nextLines[0])) nextLines.shift();
  const body = nextLines.join('\n').trim();
  return body ? { title, body } : { title, body: content.trim() };
}

export function extractCanvasDeliverable(
  rawContent: string,
  options: {
    kind?: CanvasObjectKind;
    fallbackTitle?: string;
    sourceMessageId?: string;
    preserveTitle?: boolean;
  } = {},
): CanvasExtractedDocument {
  const kind = options.kind ?? 'article';
  const object = getCanvasObject(kind);
  const replacement = extractCanvasReplacement(rawContent);
  const base = stripOuterCodeFence((replacement ?? rawContent).replace(/\r\n/g, '\n'));
  const cleaned = stripTrailingAssistantMeta(
    stripLeadingAssistantMeta(stripLeadingSeparatorPrelude(base)),
  );
  const { title: extractedTitle, body } = options.preserveTitle
    ? { title: null, body: cleaned }
    : extractTitleAndBody(cleaned);
  const fallbackTitle = isGenericCanvasTitle(options.fallbackTitle, kind)
    ? object.defaultTitle
    : normalizeTitle(options.fallbackTitle ?? '');

  return {
    kind,
    title: extractedTitle || fallbackTitle || object.defaultTitle,
    content: body || cleaned || base,
    sourceMessageId: options.sourceMessageId,
  };
}

export function prepareCanvasOpenSeed(seed: CanvasOpenSeed): CanvasExtractedDocument {
  return extractCanvasDeliverable(seed.content, {
    kind: seed.kind ?? 'article',
    fallbackTitle: seed.title,
    sourceMessageId: seed.sourceMessageId,
  });
}

export function sanitizeCanvasDraft(draft: CanvasDocumentDraft): CanvasDocumentDraft {
  const extracted = extractCanvasDeliverable(draft.content, {
    kind: draft.kind,
    fallbackTitle: draft.title,
    sourceMessageId: draft.sourceMessageId,
    preserveTitle: !isGenericCanvasTitle(draft.title, draft.kind),
  });
  if (extracted.title === draft.title && extracted.content === draft.content) return draft;
  return {
    ...draft,
    title: extracted.title,
    content: extracted.content,
    updatedAt: new Date().toISOString(),
  };
}

export function applyCanvasReplacement(
  draft: CanvasDocumentDraft,
  request: CanvasEditRequest,
  replacement: string,
): CanvasDocumentDraft {
  if (request.scope === 'selection' && request.selection) {
    const { start, end, text } = request.selection;
    if (draft.content.slice(start, end) !== text) {
      const fallbackStart = draft.content.indexOf(text);
      if (fallbackStart === -1) return draft;
      return {
        ...draft,
        content: `${draft.content.slice(0, fallbackStart)}${replacement}${draft.content.slice(
          fallbackStart + text.length,
        )}`,
        updatedAt: new Date().toISOString(),
      };
    }
    return {
      ...draft,
      content: `${draft.content.slice(0, start)}${replacement}${draft.content.slice(end)}`,
      updatedAt: new Date().toISOString(),
    };
  }

  return {
    ...draft,
    content: replacement,
    updatedAt: new Date().toISOString(),
  };
}
