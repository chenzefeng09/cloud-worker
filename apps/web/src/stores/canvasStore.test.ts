/**
 * canvasStore tests — Zustand actions wrapping api.canvas.*.
 *
 * The api client is mocked module-wide; 409 handling is exercised by
 * rejecting with a plain `{ status: 409, message: '...' }` object (the store
 * duck-types on `.status`/`.message` rather than `instanceof ApiError`, so no
 * real ApiError construction is needed here).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { useCanvasStore } from './canvasStore';
import type { CanvasDocument, CanvasSuggestion } from '../api/types';

const mockGet = vi.fn();
const mockCreate = vi.fn();
const mockEdit = vi.fn();
const mockAccept = vi.fn();
const mockReject = vi.fn();
const mockRevert = vi.fn();
const mockSaveVersion = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    canvas: {
      get: (...args: unknown[]) => mockGet(...args),
      create: (...args: unknown[]) => mockCreate(...args),
      edit: (...args: unknown[]) => mockEdit(...args),
      accept: (...args: unknown[]) => mockAccept(...args),
      reject: (...args: unknown[]) => mockReject(...args),
      revert: (...args: unknown[]) => mockRevert(...args),
      saveVersion: (...args: unknown[]) => mockSaveVersion(...args),
    },
  },
}));

function makeCanvas(overrides: Partial<CanvasDocument> = {}): CanvasDocument {
  return {
    id: 'cvs_1',
    session_id: 'ses_1',
    kind: 'article',
    title: '测试文档',
    current_version: 1,
    source_message_id: null,
    source_run_id: null,
    blocks: [{ id: 'b1', type: 'paragraph', text: '正文' }],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeSuggestion(overrides: Partial<CanvasSuggestion> = {}): CanvasSuggestion {
  return {
    id: 'sug_1',
    canvas_id: 'cvs_1',
    base_version: 1,
    status: 'proposed',
    instruction: '润色',
    diffs: [{ block_id: 'b1', op: 'replace', orig: '正文', sugg: '新正文' }],
    change_summary: null,
    created_at: '2026-01-01T00:00:00Z',
    resolved_at: null,
    ...overrides,
  };
}

describe('canvasStore', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useCanvasStore.getState().reset();
  });

  it('fetchCanvas success caches the canvas', async () => {
    const canvas = makeCanvas();
    mockGet.mockResolvedValueOnce(canvas);

    const result = await useCanvasStore.getState().fetchCanvas('cvs_1');

    expect(result).toEqual(canvas);
    expect(useCanvasStore.getState().canvasesById['cvs_1']).toEqual(canvas);
  });

  it('createFromMessage success caches + returns the canvas', async () => {
    const canvas = makeCanvas();
    mockCreate.mockResolvedValueOnce(canvas);

    const result = await useCanvasStore
      .getState()
      .createFromMessage('ses_1', 'msg_1', { kind: 'article' });

    expect(result).toEqual(canvas);
    expect(useCanvasStore.getState().canvasesById['cvs_1']).toEqual(canvas);
    expect(mockCreate).toHaveBeenCalledWith('ses_1', {
      source_message_id: 'msg_1',
      kind: 'article',
      title: undefined,
    });
  });

  it('requestEdit success stores the suggestion and sends base_version = cached current_version', async () => {
    const canvas = makeCanvas({ current_version: 3 });
    useCanvasStore.getState().setCanvas(canvas);
    const suggestion = makeSuggestion({ base_version: 3 });
    mockEdit.mockResolvedValueOnce({ suggestion });

    const result = await useCanvasStore.getState().requestEdit('cvs_1', 'ses_1', {
      instruction: '润色',
      scope: { type: 'document' },
    });

    expect(result).toEqual(suggestion);
    expect(useCanvasStore.getState().suggestionsByCanvasId['cvs_1']).toEqual(suggestion);
    expect(mockEdit).toHaveBeenCalledWith(
      'ses_1',
      'cvs_1',
      expect.objectContaining({ base_version: 3 }),
    );
  });

  it('requestEdit 409 does not set a suggestion, sets an error, and returns null', async () => {
    const canvas = makeCanvas({ current_version: 2 });
    useCanvasStore.getState().setCanvas(canvas);
    mockEdit.mockRejectedValueOnce({ status: 409, message: '请刷新' });

    const result = await useCanvasStore.getState().requestEdit('cvs_1', 'ses_1', {
      instruction: '润色',
      scope: { type: 'document' },
    });

    expect(result).toBeNull();
    expect(useCanvasStore.getState().suggestionsByCanvasId['cvs_1']).toBeFalsy();
    expect(useCanvasStore.getState().errorByCanvasId['cvs_1']).toBeTruthy();
  });

  it('applySuggestion accept flow caches the new version and clears the suggestion', async () => {
    const original = makeCanvas({ current_version: 1 });
    useCanvasStore.getState().setCanvas(original);
    useCanvasStore.getState().setSuggestion('cvs_1', makeSuggestion());
    const updated = makeCanvas({ current_version: 2 });
    mockAccept.mockResolvedValueOnce(updated);

    const result = await useCanvasStore.getState().applySuggestion('cvs_1', 'sug_1', true);

    expect(result).toEqual(updated);
    expect(useCanvasStore.getState().canvasesById['cvs_1'].current_version).toBe(2);
    expect(useCanvasStore.getState().suggestionsByCanvasId['cvs_1']).toBeNull();
  });

  it('saveManual 409 sets an error and returns null', async () => {
    const canvas = makeCanvas({ current_version: 5 });
    useCanvasStore.getState().setCanvas(canvas);
    mockSaveVersion.mockRejectedValueOnce({ status: 409, message: '请刷新' });

    const result = await useCanvasStore
      .getState()
      .saveManual('cvs_1', canvas.blocks, 5);

    expect(result).toBeNull();
    expect(useCanvasStore.getState().errorByCanvasId['cvs_1']).toBeTruthy();
  });
});
