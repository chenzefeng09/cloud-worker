// ── "@ file" mention trigger detection + helpers ─────────────────────────────
//
// Pure, DOM-free helpers powering the composer's "@ file" autocomplete. Kept in
// their own module (rather than inline in InputBar.tsx) so the trigger logic can
// be unit-tested without mounting the component.

export type MentionKind = 'workspace' | 'attachment';

/** A file the user can insert via "@": a workspace base file or a session input attachment. */
export interface MentionCandidate {
  id: string;
  filename: string;
  kind: MentionKind;
  /** Best-effort MIME (from the source listing); optional — preview falls back to the extension. */
  mime?: string;
}

/** A recorded "@filename" reference sitting in the composer text. */
export interface MentionReference {
  id: string;
  filename: string;
  kind: MentionKind;
}

export interface MentionTrigger {
  /** Index of the triggering '@' character in the text. */
  at: number;
  /** Text between the '@' and the cursor (the live filter query). */
  query: string;
}

/**
 * Detect whether an "@ file" popover should be active for the given cursor.
 *
 * Rules:
 *   - Scan backwards from the cursor for the NEAREST '@'.
 *   - A newline between the '@' and the cursor cancels the trigger (a mention
 *     query never spans lines).
 *   - The '@' must sit at the start of a line or immediately after whitespace,
 *     so "foo@bar.com" (an email) never triggers.
 *
 * Returns the anchor index + the filter query, or null when no trigger applies.
 */
export function detectMentionTrigger(text: string, cursor: number): MentionTrigger | null {
  if (cursor < 0 || cursor > text.length) return null;
  for (let i = cursor - 1; i >= 0; i--) {
    const ch = text[i];
    if (ch === '\n') return null; // query cannot span a newline
    if (ch === '@') {
      const before = i === 0 ? '' : text[i - 1];
      const atLineStart = i === 0 || before === '\n';
      const afterWhitespace = before === ' ' || before === '\t';
      if (atLineStart || afterWhitespace) {
        return { at: i, query: text.slice(i + 1, cursor) };
      }
      // '@' glued to a non-whitespace char (e.g. an email) — not a mention.
      return null;
    }
  }
  return null;
}

/** Case-insensitive `filename.includes(query)` filter over the candidate list. */
export function filterMentionCandidates(
  candidates: MentionCandidate[],
  query: string,
): MentionCandidate[] {
  const q = query.toLowerCase();
  if (!q) return candidates.slice();
  return candidates.filter((c) => c.filename.toLowerCase().includes(q));
}

/**
 * Of the recorded references, return the ids of the `workspace`-kind ones whose
 * "@filename" token still survives in the composer text (the user may have
 * deleted it). Attachment-kind references are intentionally excluded — the
 * backend injects all session attachments already, so they need no id list.
 */
export function activeWorkspaceFileIds(
  refs: MentionReference[],
  text: string,
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const r of refs) {
    if (r.kind !== 'workspace') continue;
    if (seen.has(r.id)) continue;
    if (text.includes(`@${r.filename}`)) {
      seen.add(r.id);
      out.push(r.id);
    }
  }
  return out;
}
