/**
 * Pre-session ("draft") composer selections and their handover to a real
 * session id.
 *
 * The home composer has no session when the user opens the "+" menu — one is
 * created lazily on send. Toggles therefore land under DRAFT_SESSION_ID and are
 * committed to the new session by `commitDraftSelections`. The delicate part is
 * that the three-state contract (absent / [] / non-empty) has to survive that
 * handover: `[]` means "everything off" and must NOT be re-seeded back to the
 * defaults.
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { DRAFT_SESSION_ID, useChatStore } from './chatStore';

beforeEach(() => {
  useChatStore.setState({ selectedSkills: {}, selectedMcpServerIds: {} });
});

describe('commitDraftSelections', () => {
  it('moves the draft onto the new session id', () => {
    const store = useChatStore.getState();
    store.seedSkills(DRAFT_SESSION_ID, ['pdf-report']);
    store.seedMcpServerIds(DRAFT_SESSION_ID, ['mcp_a', 'mcp_b']);

    useChatStore.getState().commitDraftSelections('ses_1');

    const s = useChatStore.getState();
    expect(s.selectedSkills.ses_1).toEqual(['pdf-report']);
    expect(s.selectedMcpServerIds.ses_1).toEqual(['mcp_a', 'mcp_b']);
  });

  it('clears the draft so the next new session does not inherit it', () => {
    const store = useChatStore.getState();
    store.seedSkills(DRAFT_SESSION_ID, ['pdf-report']);
    store.seedMcpServerIds(DRAFT_SESSION_ID, ['mcp_a']);
    useChatStore.getState().commitDraftSelections('ses_1');

    let s = useChatStore.getState();
    expect(s.selectedSkills[DRAFT_SESSION_ID]).toBeUndefined();
    expect(s.selectedMcpServerIds[DRAFT_SESSION_ID]).toBeUndefined();

    // Second task started from the home page without touching the menu.
    useChatStore.getState().commitDraftSelections('ses_2');
    s = useChatStore.getState();
    expect(s.selectedSkills.ses_2).toBeUndefined();
    expect(s.selectedMcpServerIds.ses_2).toBeUndefined();
    // ...and the first session keeps what it was given.
    expect(s.selectedSkills.ses_1).toEqual(['pdf-report']);
  });

  it('keeps an explicit [] distinct from an untouched draft', () => {
    // MCP explicitly emptied ("all off"), skills never touched.
    const store = useChatStore.getState();
    store.seedMcpServerIds(DRAFT_SESSION_ID, ['mcp_a']);
    store.toggleMcpServer(DRAFT_SESSION_ID, 'mcp_a');
    expect(useChatStore.getState().selectedMcpServerIds[DRAFT_SESSION_ID]).toEqual([]);

    useChatStore.getState().commitDraftSelections('ses_1');

    const s = useChatStore.getState();
    expect(s.selectedMcpServerIds.ses_1).toEqual([]);
    expect('ses_1' in s.selectedMcpServerIds).toBe(true);
    // Untouched half stays absent — it must not become [] on the way over.
    expect('ses_1' in s.selectedSkills).toBe(false);
  });

  it('drops the draft even when only one half was touched', () => {
    useChatStore.getState().seedSkills(DRAFT_SESSION_ID, ['pdf-report']);
    useChatStore.getState().commitDraftSelections('ses_1');

    expect(useChatStore.getState().selectedSkills[DRAFT_SESSION_ID]).toBeUndefined();
  });

  it('never clobbers a selection the session already made', () => {
    const store = useChatStore.getState();
    store.seedSkills('ses_1', ['already-chosen']);
    store.seedSkills(DRAFT_SESSION_ID, ['from-draft']);

    useChatStore.getState().commitDraftSelections('ses_1');

    expect(useChatStore.getState().selectedSkills.ses_1).toEqual(['already-chosen']);
    expect(useChatStore.getState().selectedSkills[DRAFT_SESSION_ID]).toBeUndefined();
  });

  it('is a no-op when nothing was drafted', () => {
    const before = useChatStore.getState().selectedSkills;
    useChatStore.getState().commitDraftSelections('ses_1');
    expect(useChatStore.getState().selectedSkills).toBe(before);
  });
});

describe('logout', () => {
  it('clears the draft slot along with the per-session selections', () => {
    const store = useChatStore.getState();
    store.seedSkills(DRAFT_SESSION_ID, ['pdf-report']);
    store.seedMcpServerIds(DRAFT_SESSION_ID, ['mcp_a']);
    store.seedSkills('ses_1', ['other']);

    useChatStore.getState().logout();

    const s = useChatStore.getState();
    expect(s.selectedSkills).toEqual({});
    expect(s.selectedMcpServerIds).toEqual({});
  });
});
