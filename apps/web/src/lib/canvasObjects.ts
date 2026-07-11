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

export interface CanvasAuthoringIntent {
  kind: CanvasObjectKind;
  title: string;
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
    label: '文章',
    description: '公众号、博客、长文稿件',
    defaultTitle: '未命名文章',
    starter:
      '标题\n\n开头：用一两句话说明这篇文章要解决的问题。\n\n一、核心观点\n\n二、展开分析\n\n三、行动建议\n\n结尾：给读者一个明确的收束。',
    editIntents: [
      { id: 'polish', label: '润色', instruction: '在保留原意的前提下，让表达更清晰、自然、有节奏。' },
      { id: 'expand', label: '扩写', instruction: '补充论据和过渡，让内容更完整。' },
      { id: 'shorten', label: '压缩', instruction: '删去冗余表达，保留关键信息。' },
    ],
  },
  {
    kind: 'brief',
    label: '简报',
    description: '会议纪要、项目同步、决策摘要',
    defaultTitle: '工作简报',
    starter:
      '背景\n\n关键进展\n\n风险与阻塞\n\n下一步\n\n需要决策',
    editIntents: [
      { id: 'executive', label: '高管摘要', instruction: '改成高管可快速扫描的摘要，突出结论、风险和动作。' },
      { id: 'actionable', label: '行动化', instruction: '把内容改成明确的负责人、动作和截止时间。' },
    ],
  },
  {
    kind: 'email',
    label: '邮件',
    description: '客户、供应商、内部沟通',
    defaultTitle: '邮件草稿',
    starter:
      '主题：\n\n你好，\n\n正文\n\n谢谢，\n',
    editIntents: [
      { id: 'warmer', label: '更友好', instruction: '语气更友好、专业，但不要啰嗦。' },
      { id: 'firmer', label: '更坚定', instruction: '语气更明确坚定，同时保持礼貌。' },
    ],
  },
  {
    kind: 'report',
    label: '报告',
    description: '调研、复盘、分析报告',
    defaultTitle: '分析报告',
    starter:
      '摘要\n\n背景\n\n发现\n\n分析\n\n建议\n\n附录',
    editIntents: [
      { id: 'structured', label: '结构化', instruction: '重组结构，使标题层级、论证顺序和结论更清楚。' },
      { id: 'evidence', label: '补证据', instruction: '指出哪里需要数据、案例或引用支撑，并补出可写入的表达。' },
    ],
  },
  {
    kind: 'proposal',
    label: '方案',
    description: '产品、项目、商业方案',
    defaultTitle: '方案草稿',
    starter:
      '目标\n\n现状与问题\n\n方案设计\n\n实施路径\n\n成本与风险\n\n验收标准',
    editIntents: [
      { id: 'sharpen', label: '强化说服力', instruction: '强化价值主张、取舍理由和落地路径。' },
      { id: 'risks', label: '补风险', instruction: '补充风险、约束、前置条件和兜底方案。' },
    ],
  },
  {
    kind: 'social_post',
    label: '短内容',
    description: '小红书、朋友圈、社媒帖',
    defaultTitle: '短内容草稿',
    starter:
      '开头钩子：\n\n正文：\n\n互动问题：',
    editIntents: [
      { id: 'hook', label: '强化开头', instruction: '改写开头，让前三秒更抓人，但不要夸张失真。' },
      { id: 'punchy', label: '更短更有力', instruction: '改得更短、更口语、更有记忆点。' },
    ],
  },
  {
    kind: 'code_note',
    label: '技术说明',
    description: 'README、设计说明、接口文档',
    defaultTitle: '技术说明',
    starter:
      '背景\n\n目标\n\n接口/模块\n\n关键流程\n\n边界条件\n\n测试与验证',
    editIntents: [
      { id: 'precise', label: '更精确', instruction: '修正含糊表述，让技术边界、输入输出和失败场景更明确。' },
      { id: 'readme', label: 'README 化', instruction: '改成开发者容易上手的 README 风格。' },
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
    .replace(/^标题\s*[:：]\s*/, '')
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

  const labelled = trimmed.match(/^标题\s*[:：]\s*(.+)$/);
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

function inferCanvasKind(prompt: string): CanvasObjectKind | null {
  const text = prompt.toLowerCase();
  if (/(代码|函数|脚本|sql|程序)/i.test(prompt) && !/(文档|说明|readme|设计)/i.test(prompt)) {
    return null;
  }
  if (/(邮件|email|mail)/i.test(prompt)) return 'email';
  if (/(报告|调研|分析|复盘)/i.test(prompt)) return 'report';
  if (/(方案|计划书|proposal|项目计划)/i.test(prompt)) return 'proposal';
  if (/(简报|纪要|周报|日报|同步|brief)/i.test(prompt)) return 'brief';
  if (/(小红书|朋友圈|微博|推文|帖子|社媒|social)/i.test(prompt)) return 'social_post';
  if (/(readme|接口文档|技术文档|设计说明|技术说明|api\s*doc)/i.test(text)) return 'code_note';
  if (/(文章|长文|科普|博客|公众号|稿件|essay|article|post)/i.test(prompt)) return 'article';
  return null;
}

function inferCanvasTitle(prompt: string, kind: CanvasObjectKind): string {
  const object = getCanvasObject(kind);
  const normalized = prompt.replace(/\s+/g, ' ').trim();
  const topicMatch = normalized.match(
    /(?:关于|有关|围绕|以)(.+?)(?:的)?(?:科普文章|文章|长文|博客|公众号|报告|邮件|方案|简报|帖子|文案|内容|$)/,
  );
  const topic = topicMatch?.[1]?.trim().replace(/[，。,.!?！？]+$/g, '');
  if (topic) return `关于${topic}的${object.label}`;

  const cleaned = normalized
    .replace(/^(请|帮我|给我|麻烦你|麻烦)?\s*/i, '')
    .replace(/^(写|撰写|起草|生成|创作|整理|输出)\s*/i, '')
    .replace(/^(一篇|一份|一个|一下)\s*/i, '')
    .replace(/[，。,.!?！？]+$/g, '')
    .trim();
  if (!cleaned) return object.defaultTitle;
  return cleaned.length > 36 ? `${cleaned.slice(0, 36)}…` : cleaned;
}

export function inferCanvasAuthoringIntent(prompt: string): CanvasAuthoringIntent | null {
  const normalized = prompt.trim();
  if (!normalized) return null;
  const hasAuthoringVerb = /(写|撰写|起草|生成|创作|草拟|整理成|输出|draft|write|create|generate)/i.test(
    normalized,
  );
  if (!hasAuthoringVerb) return null;
  const kind = inferCanvasKind(normalized);
  if (!kind) return null;
  return {
    kind,
    title: inferCanvasTitle(normalized, kind),
  };
}
