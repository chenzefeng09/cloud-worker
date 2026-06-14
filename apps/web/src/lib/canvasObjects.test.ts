import { describe, expect, it } from 'vitest';
import {
  applyCanvasReplacement,
  extractCanvasDeliverable,
  prepareCanvasOpenSeed,
  sanitizeCanvasDraft,
  type CanvasDocumentDraft,
  type CanvasEditRequest,
} from './canvasObjects';

function draft(content: string): CanvasDocumentDraft {
  return {
    id: 'canvas_test',
    kind: 'article',
    title: '测试文章',
    content,
    updatedAt: '2026-06-14T00:00:00.000Z',
  };
}

function selectionRequest(content: string, text: string, start: number): CanvasEditRequest {
  return {
    documentId: 'canvas_test',
    kind: 'article',
    title: '测试文章',
    scope: 'selection',
    instruction: '修改选区',
    content,
    selection: {
      text,
      start,
      end: start + text.length,
    },
  };
}

describe('applyCanvasReplacement', () => {
  it('replaces the selected range when offsets still match', () => {
    const source = draft('开头文字。需要修改。结尾文字。');
    const request = selectionRequest(source.content, '需要修改', source.content.indexOf('需要修改'));

    const result = applyCanvasReplacement(source, request, '已经修改');

    expect(result.content).toBe('开头文字。已经修改。结尾文字。');
  });

  it('falls back to text matching when saved offsets are stale', () => {
    const source = draft('前面新增。开头文字。需要修改。结尾文字。');
    const request = selectionRequest('开头文字。需要修改。结尾文字。', '需要修改', 5);

    const result = applyCanvasReplacement(source, request, '已经修改');

    expect(result.content).toBe('前面新增。开头文字。已经修改。结尾文字。');
  });

  it('does not change the draft when a stale selection can no longer be found', () => {
    const source = draft('内容已经被用户手动改掉。');
    const request = selectionRequest('开头文字。需要修改。结尾文字。', '需要修改', 5);

    const result = applyCanvasReplacement(source, request, '已经修改');

    expect(result).toBe(source);
  });

  it('replaces the full document for document-scope requests', () => {
    const source = draft('旧正文');
    const request: CanvasEditRequest = {
      documentId: source.id,
      kind: source.kind,
      title: source.title,
      scope: 'document',
      instruction: '重写全文',
      content: source.content,
    };

    const result = applyCanvasReplacement(source, request, '新正文');

    expect(result.content).toBe('新正文');
  });
});

describe('extractCanvasDeliverable', () => {
  it('keeps only the article when the assistant reply includes preface and notes', () => {
    const result = extractCanvasDeliverable(
      [
        '写国旗下讲话需要先确认几个关键信息，才能让稿件更贴合实际使用场景：',
        '',
        '好的，面向小学开学场景，我来写一篇适合小学生聆听、时长约3分钟的国旗下讲话稿。',
        '',
        '---',
        '',
        '新学期，做更好的自己',
        '',
        '亲爱的老师们、同学们：',
        '',
        '大家早上好！',
        '',
        '迎着初升的太阳，我们又回到了熟悉的校园。',
        '',
        '谢谢大家！',
        '',
        '---',
        '',
        '时长说明：以上稿件按正常语速朗读约3分钟，适合小学开学典礼使用。',
      ].join('\n'),
      { kind: 'article', fallbackTitle: 'AI 回答草稿' },
    );

    expect(result.title).toBe('新学期，做更好的自己');
    expect(result.content).toContain('亲爱的老师们、同学们：');
    expect(result.content).toContain('谢谢大家！');
    expect(result.content).not.toContain('写国旗下讲话需要先确认');
    expect(result.content).not.toContain('面向小学开学场景');
    expect(result.content).not.toContain('时长说明');
    expect(result.content).not.toContain('新学期，做更好的自己');
  });

  it('uses markdown headings as the canvas title and removes duplicate title text from body', () => {
    const result = extractCanvasDeliverable(
      [
        '# 周一A股，会不会迎来变盘？',
        '',
        '这个周末，很多股民过得并不轻松。',
        '',
        '市场最缺的，不是利好。',
      ].join('\n'),
      { kind: 'article' },
    );

    expect(result.title).toBe('周一A股，会不会迎来变盘？');
    expect(result.content).toBe('这个周末，很多股民过得并不轻松。\n\n市场最缺的，不是利好。');
  });

  it('does not mistake a speech salutation for a title', () => {
    const result = extractCanvasDeliverable(
      [
        '亲爱的老师们、同学们：',
        '',
        '大家早上好！',
      ].join('\n'),
      { kind: 'article', fallbackTitle: '写一篇国旗下讲话，3分钟' },
    );

    expect(result.title).toBe('写一篇国旗下讲话，3分钟');
    expect(result.content.startsWith('亲爱的老师们、同学们：')).toBe(true);
  });

  it('normalizes manual canvas seeds instead of preserving the generic draft title', () => {
    const result = prepareCanvasOpenSeed({
      kind: 'article',
      title: 'AI 回答草稿',
      content: '标题：开盘前的市场观察\n\n过去一段时间，市场给人的感觉始终有些沉闷。',
      sourceMessageId: 'msg_1',
    });

    expect(result.title).toBe('开盘前的市场观察');
    expect(result.content).toBe('过去一段时间，市场给人的感觉始终有些沉闷。');
    expect(result.sourceMessageId).toBe('msg_1');
  });

  it('removes bold markdown duration notes at the end of an existing article', () => {
    const result = extractCanvasDeliverable(
      [
        '亲爱的老师们、同学们：',
        '',
        '大家早上好！',
        '',
        '谢谢大家！',
        '',
        '---',
        '',
        '**时长说明：** 以上稿件按正常语速朗读约 **3分钟**（约600字），适合小学开学典礼使用。',
      ].join('\n'),
      { kind: 'article', fallbackTitle: '新学期，做更好的自己' },
    );

    expect(result.title).toBe('新学期，做更好的自己');
    expect(result.content).toBe('亲爱的老师们、同学们：\n\n大家早上好！\n\n谢谢大家！');
  });

  it('removes duration notes when the separator is not surrounded by blank lines', () => {
    const result = extractCanvasDeliverable(
      [
        '亲爱的老师们、同学们：',
        '',
        '大家早上好！',
        '',
        '谢谢大家！',
        '---',
        '**时长说明：** 以上稿件按正常语速朗读约 **3分钟**（约600字），适合小学开学典礼使用。如果需要调整篇幅或风格（更活泼/更庄重），随时告诉我！',
      ].join('\n'),
      { kind: 'article', fallbackTitle: '新学期，做更好的自己' },
    );

    expect(result.content).toBe('亲爱的老师们、同学们：\n\n大家早上好！\n\n谢谢大家！');
    expect(result.content).not.toContain('时长说明');
    expect(result.content).not.toContain('随时告诉我');
    expect(result.content).not.toContain('---');
  });

  it('sanitizes stored drafts that already contain assistant meta notes', () => {
    const source = draft(
      [
        '亲爱的老师们、同学们：',
        '',
        '大家早上好！',
        '',
        '谢谢大家！',
        '',
        '---',
        '',
        '**时长说明：** 以上稿件按正常语速朗读约 **3分钟**。',
      ].join('\n'),
    );

    const result = sanitizeCanvasDraft(source);

    expect(result.content).not.toContain('时长说明');
    expect(result.content).not.toContain('---');
    expect(result.title).toBe(source.title);
  });

  it('keeps section headings when sanitizing an already titled draft', () => {
    const source = draft(
      [
        '引言',
        '',
        '这是正文第一段。',
        '',
        '谢谢大家！',
        '---',
        '时长说明：以上稿件按正常语速朗读约3分钟。',
      ].join('\n'),
    );

    const result = sanitizeCanvasDraft(source);

    expect(result.title).toBe('测试文章');
    expect(result.content).toBe('引言\n\n这是正文第一段。\n\n谢谢大家！');
  });
});
