import { describe, expect, it } from 'vitest';
import type { CanvasBlock } from '../api/types';
import {
  canvasBlocksToHtml,
  canvasEditorToBlocks,
  getCanvasBlockSelectionsInside,
} from './canvasRichText';

const blocks: CanvasBlock[] = [
  { id: 'blk_1', type: 'paragraph', text: '第一段原文。' },
  { id: 'blk_2', type: 'paragraph', text: '第二段需要修改。' },
];

function editorFor(source: CanvasBlock[] = blocks): HTMLDivElement {
  const editor = document.createElement('div');
  editor.innerHTML = canvasBlocksToHtml(source);
  document.body.appendChild(editor);
  return editor;
}

describe('canvas block rich text', () => {
  it('keeps stable block ids when an earlier block is deleted', () => {
    const editor = editorFor();
    editor.querySelector('[data-canvas-block-id="blk_1"]')?.remove();

    const result = canvasEditorToBlocks(editor, blocks, () => 'blk_new');

    expect(result).toEqual([
      { id: 'blk_2', type: 'paragraph', text: '第二段需要修改。' },
    ]);
    editor.remove();
  });

  it('resolves a browser selection to an exact canonical block range', () => {
    const editor = editorFor();
    const block = editor.querySelector<HTMLElement>('[data-canvas-block-id="blk_2"]');
    const text = block?.querySelector('p')?.firstChild;
    if (!(text instanceof Text)) throw new Error('test text node missing');

    const selected = '需要修改';
    const start = text.data.indexOf(selected);
    const range = document.createRange();
    range.setStart(text, start);
    range.setEnd(text, start + selected.length);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);

    expect(getCanvasBlockSelectionsInside(editor, blocks)).toEqual([
      {
        block_id: 'blk_2',
        start: blocks[1].text.indexOf(selected),
        end: blocks[1].text.indexOf(selected) + selected.length,
        text: selected,
      },
    ]);
    editor.remove();
  });
});
