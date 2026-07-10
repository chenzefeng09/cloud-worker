/**
 * Canvas v2 store — caches server canvas documents + current suggestions and
 * wraps `api.canvas.*` in async actions.
 *
 * Styled after chatStore.ts. STRICT selector-safety: all state is plain
 * records keyed by canvas id; every mutator returns a new top-level record via
 * immutable spread (`{ ...prev, [id]: value }`) rather than mutating in place,
 * and no selector here builds a fresh object/array — that pattern is what
 * causes the "selector returns fresh object → infinite render loop" pitfall
 * documented in ChatWindow.tsx (see EMPTY_INTERACTIONS).
 *
 * 409 handling uses duck-typing (`(err as { status?: number }).status`)
 * instead of `instanceof ApiError` so tests can throw a plain
 * `{ status: 409, message: '...' }` without needing the real ApiError class.
 */
import { create } from 'zustand';
import { api } from '../api/client';
import type { CanvasBlock, CanvasDocument, CanvasSuggestion } from '../api/types';

/** Duck-typed error shape — matches ApiError's public fields without importing the class. */
function errorStatus(err: unknown): number | undefined {
  if (err && typeof err === 'object' && 'status' in err) {
    const status = (err as { status?: unknown }).status;
    return typeof status === 'number' ? status : undefined;
  }
  return undefined;
}

function errorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    const message = (err as { message?: unknown }).message;
    if (typeof message === 'string' && message) return message;
  }
  return '请求失败';
}

const REFRESH_STALE_MESSAGE = '画布已被更新，请刷新后重试';

export interface CanvasState {
  // server object cache, keyed by canvas id
  canvasesById: Record<string, CanvasDocument>;
  // at most one active suggestion per canvas (null = none)
  suggestionsByCanvasId: Record<string, CanvasSuggestion | null>;
  // canvas id currently shown in the side panel (null = none)
  panelCanvasId: string | null;
  // canvas ids with an edit/accept/revert/save in flight
  revisingCanvasIds: Record<string, boolean>;
  // last localized error per canvas (from ApiError.message / user_message)
  errorByCanvasId: Record<string, string | null>;

  // --- setters ---
  setPanelCanvasId: (id: string | null) => void;
  setCanvas: (canvas: CanvasDocument) => void;
  setSuggestion: (canvasId: string, s: CanvasSuggestion | null) => void;
  clearError: (canvasId: string) => void;
  reset: () => void;

  // --- async actions ---
  fetchCanvas: (canvasId: string) => Promise<CanvasDocument | null>;
  createFromMessage: (
    sessionId: string,
    sourceMessageId: string,
    opts?: { kind?: string; title?: string },
  ) => Promise<CanvasDocument>;
  createFromContent: (
    sessionId: string,
    content: string,
    opts?: { kind?: string; title?: string },
  ) => Promise<CanvasDocument>;
  requestEdit: (
    canvasId: string,
    sessionId: string,
    req: { instruction: string; scope: { type: 'document' | 'blocks'; block_ids?: string[] }; model?: string },
  ) => Promise<CanvasSuggestion | null>;
  applySuggestion: (
    canvasId: string,
    suggestionId: string,
    accept: boolean,
  ) => Promise<CanvasDocument | null>;
  revert: (canvasId: string, toVersion: number) => Promise<CanvasDocument | null>;
  saveManual: (
    canvasId: string,
    blocks: CanvasBlock[],
    baseVersion: number,
  ) => Promise<CanvasDocument | null>;
}

const INITIAL_STATE = {
  canvasesById: {} as Record<string, CanvasDocument>,
  suggestionsByCanvasId: {} as Record<string, CanvasSuggestion | null>,
  panelCanvasId: null as string | null,
  revisingCanvasIds: {} as Record<string, boolean>,
  errorByCanvasId: {} as Record<string, string | null>,
};

export const useCanvasStore = create<CanvasState>((set, get) => ({
  ...INITIAL_STATE,

  setPanelCanvasId: (id) => set({ panelCanvasId: id }),

  setCanvas: (canvas) =>
    set((s) => ({ canvasesById: { ...s.canvasesById, [canvas.id]: canvas } })),

  setSuggestion: (canvasId, sug) =>
    set((s) => ({
      suggestionsByCanvasId: { ...s.suggestionsByCanvasId, [canvasId]: sug },
    })),

  clearError: (canvasId) =>
    set((s) => ({ errorByCanvasId: { ...s.errorByCanvasId, [canvasId]: null } })),

  reset: () => set({ ...INITIAL_STATE }),

  fetchCanvas: async (canvasId) => {
    set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: true } }));
    try {
      const canvas = await api.canvas.get(canvasId);
      get().setCanvas(canvas);
      return canvas;
    } catch (err) {
      set((s) => ({
        errorByCanvasId: { ...s.errorByCanvasId, [canvasId]: errorMessage(err) },
      }));
      return null;
    } finally {
      set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: false } }));
    }
  },

  createFromMessage: async (sessionId, sourceMessageId, opts) => {
    try {
      const canvas = await api.canvas.create(sessionId, {
        source_message_id: sourceMessageId,
        kind: opts?.kind,
        title: opts?.title,
      });
      get().setCanvas(canvas);
      return canvas;
    } catch (err) {
      // Fail-open contract: caller (AgentPage) falls back to the chat bubble
      // and console.warns — this must reject, not swallow.
      throw err;
    }
  },

  createFromContent: async (sessionId, content, opts) => {
    try {
      const canvas = await api.canvas.create(sessionId, {
        content,
        kind: opts?.kind,
        title: opts?.title,
      });
      get().setCanvas(canvas);
      return canvas;
    } catch (err) {
      throw err;
    }
  },

  requestEdit: async (canvasId, sessionId, req) => {
    set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: true } }));
    try {
      const canvas = get().canvasesById[canvasId];
      const base_version = canvas?.current_version;
      const res = await api.canvas.edit(sessionId, canvasId, {
        instruction: req.instruction,
        scope: req.scope,
        model: req.model,
        base_version,
      });
      get().setSuggestion(canvasId, res.suggestion);
      return res.suggestion;
    } catch (err) {
      const status = errorStatus(err);
      const message = status === 409 ? REFRESH_STALE_MESSAGE : errorMessage(err);
      set((s) => ({
        errorByCanvasId: { ...s.errorByCanvasId, [canvasId]: message },
      }));
      return null;
    } finally {
      set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: false } }));
    }
  },

  applySuggestion: async (canvasId, suggestionId, accept) => {
    set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: true } }));
    try {
      if (accept) {
        const canvas = await api.canvas.accept(canvasId, suggestionId);
        get().setCanvas(canvas);
        get().setSuggestion(canvasId, null);
        return canvas;
      }
      await api.canvas.reject(canvasId, suggestionId);
      get().setSuggestion(canvasId, null);
      return null;
    } catch (err) {
      set((s) => ({
        errorByCanvasId: { ...s.errorByCanvasId, [canvasId]: errorMessage(err) },
      }));
      return null;
    } finally {
      set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: false } }));
    }
  },

  revert: async (canvasId, toVersion) => {
    set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: true } }));
    try {
      const canvas = await api.canvas.revert(canvasId, toVersion);
      get().setCanvas(canvas);
      return canvas;
    } catch (err) {
      set((s) => ({
        errorByCanvasId: { ...s.errorByCanvasId, [canvasId]: errorMessage(err) },
      }));
      return null;
    } finally {
      set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: false } }));
    }
  },

  saveManual: async (canvasId, blocks, baseVersion) => {
    set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: true } }));
    try {
      const canvas = await api.canvas.saveVersion(canvasId, {
        blocks,
        base_version: baseVersion,
      });
      get().setCanvas(canvas);
      return canvas;
    } catch (err) {
      const status = errorStatus(err);
      const message = status === 409 ? REFRESH_STALE_MESSAGE : errorMessage(err);
      set((s) => ({
        errorByCanvasId: { ...s.errorByCanvasId, [canvasId]: message },
      }));
      return null;
    } finally {
      set((s) => ({ revisingCanvasIds: { ...s.revisingCanvasIds, [canvasId]: false } }));
    }
  },
}));
