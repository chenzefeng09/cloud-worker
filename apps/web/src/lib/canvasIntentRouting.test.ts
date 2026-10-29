import { describe, it, expect } from 'vitest';
import { routeCanvasIntent } from './canvasIntentRouting';

describe('routeCanvasIntent', () => {
  describe('no active canvas → always chat', () => {
    const noCanvasCases = [
      '把这段改简洁',
      '换个标题',
      '精简第二段',
      '随便聊聊',
    ];
    for (const msg of noCanvasCases) {
      it(`"${msg}" routes to chat when no active canvas`, () => {
        expect(routeCanvasIntent(msg, false)).toBe('chat');
      });
    }
  });

  describe('active canvas + modify intent → update_canvas', () => {
    const updateCases = [
      '把这段改简洁',
      '改得更口语一点',
      '改简洁',
      '精简第二段',
      '压缩一下正文',
      '扩写第三段',
      '润色全文',
      '重写这一段',
      '改写开头',
      '换个标题',
      '换一个更抓人的标题',
      '改一下题目',
      '把标题换成更短的',
      '把第二段改成更正式的语气',
      '调整一下语气',
      '删掉最后一段',
      '优化一下表达',
    ];
    for (const msg of updateCases) {
      it(`"${msg}" routes to update_canvas`, () => {
        expect(routeCanvasIntent(msg, true)).toBe('update_canvas');
      });
    }
  });

  describe('active canvas + NOT a modify intent → chat', () => {
    const chatCases = [
      // explicit new-content signals must never be hijacked
      '帮我再写一篇类似的文章',
      '再写一份周报',
      '另写一个版本',
      '新写一篇短文',
      '重新写一篇更长的',
      '新建一个画布',
      // plain conversation, no modify verb
      '这段写得怎么样',
      '你觉得哪里可以更好',
      '谢谢',
      '解释一下第二段的意思',
      '这篇文章的主题是什么',
      '给我一些参考资料',
    ];
    for (const msg of chatCases) {
      it(`"${msg}" routes to chat`, () => {
        expect(routeCanvasIntent(msg, true)).toBe('chat');
      });
    }
  });

  it('empty / whitespace message routes to chat', () => {
    expect(routeCanvasIntent('', true)).toBe('chat');
    expect(routeCanvasIntent('   ', true)).toBe('chat');
  });

  describe('English messages', () => {
    const updateCases = [
      'Rewrite this paragraph',
      'Shorten the intro',
      'Polish the whole thing',
      'Make it more formal',
      'Change the second paragraph to a friendlier tone',
      'Replace the ending with a call to action',
      'Change the title',
      'Remove the last paragraph',
    ];
    for (const msg of updateCases) {
      it(`"${msg}" routes to update_canvas`, () => {
        expect(routeCanvasIntent(msg, true)).toBe('update_canvas');
      });
    }
    const chatCases = [
      'Write a new article on the same topic',
      'Draft another email, then polish it',
      'What would you change to improve it?',
      'Thanks!',
      'What is this article about?',
    ];
    for (const msg of chatCases) {
      it(`"${msg}" routes to chat`, () => {
        expect(routeCanvasIntent(msg, true)).toBe('chat');
      });
    }
    it('routes to chat with no active canvas', () => {
      expect(routeCanvasIntent('Rewrite this paragraph', false)).toBe('chat');
    });
  });

  it('new-content signal wins even when a modify verb is also present', () => {
    // "再写一篇" (new) + "润色" (modify) — the new-content signal must win.
    expect(routeCanvasIntent('再写一篇然后帮我润色', true)).toBe('chat');
  });
});
