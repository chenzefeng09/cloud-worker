/** Sign-out and "Delete my data" drop the browser's copies of what the user wrote. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadStoredCanvasDraft } from '../components/ArticleCanvasPanel';
import { memoryStorage } from '../components/privacy/testUtils';
import { useChatStore } from '../stores/chatStore';
import { clearLocalUserContent } from './localUserData';

const DRAFT = JSON.stringify({ id: 'cv_1', kind: 'article', title: 'Notes', content: 'private text' });

let storage: Storage;
const keys = () => Array.from({ length: storage.length }, (_, i) => storage.key(i));

beforeEach(() => {
  storage = memoryStorage();
  vi.stubGlobal('localStorage', storage);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('clearLocalUserContent', () => {
  it('removes canvas drafts and folder names but keeps UI settings', () => {
    storage.setItem('article-canvas-draft-v1:ses_1', DRAFT);
    storage.setItem('article-canvas-draft-v1:ses_2', DRAFT);
    storage.setItem('ws-empty-folders:ws_1', '["Tax returns"]');
    storage.setItem('cw.lang', 'en');

    clearLocalUserContent();

    expect(keys()).toEqual(['cw.lang']);
  });

  it('uses the key the canvas actually reads', () => {
    storage.setItem('article-canvas-draft-v1:ses_1', DRAFT);
    expect(loadStoredCanvasDraft('ses_1')?.content).toContain('private text');
    clearLocalUserContent();
    expect(loadStoredCanvasDraft('ses_1')).toBeNull();
  });

  it('runs on logout', () => {
    storage.setItem('article-canvas-draft-v1:ses_1', DRAFT);
    useChatStore.getState().logout();
    expect(storage.getItem('article-canvas-draft-v1:ses_1')).toBeNull();
  });

  it('does nothing when storage is unavailable', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(() => clearLocalUserContent()).not.toThrow();
  });
});
