/**
 * Three-state intent routing for canvas v2 (design §6.3 Phase A).
 *
 * When the session already has an **active canvas** (the most recent
 * canvas created/edited in this session), a chat-box message that is
 * clearly *modifying* that canvas ("把这段改简洁" / "换个标题" / "精简第二段")
 * must route to the EDIT path — never start a run, never post a chat
 * message, never create a second canvas block. Everything else stays in
 * the normal chat flow.
 *
 * This module owns ONLY the `update_canvas` vs `chat` decision. Creation
 * (`create_canvas`) intent detection lives in `inferCanvasAuthoringIntent`
 * (canvasObjects.ts) and is intentionally out of scope here.
 *
 * Predicate philosophy: **从窄** (narrow). The bias is to under-capture —
 * a missed update just falls through to normal chat (fail-open, the user
 * loses nothing), whereas a false-positive hijack silently swallows a
 * genuine new-content request. So the update predicate requires a modify
 * verb AND must not carry an explicit "write something new" signal.
 */

export type CanvasIntent = 'update_canvas' | 'chat';

/**
 * Explicit "start a fresh piece" signals. When any of these is present we
 * refuse to treat the message as an edit even if a modify verb also
 * appears ("重写一篇" → new article, not an edit of the current one).
 * Kept deliberately narrow: only phrasings that unambiguously mean "a new,
 * separate document".
 */
const NEW_CONTENT_SIGNAL = /再写(?:一篇|一份|一个|个|篇|份)|另写|另起(?:一篇|一份)?|新写(?:一篇|一份|一个)?|重新写(?:一篇|一份|一个)|写(?:一篇|一份|一个)(?:新的|全新)|新建(?:一个)?(?:画布|文档|文章)/;

/**
 * Modify-intent predicates. A message matching any of these (and NOT a
 * new-content signal) is an update. Verbs are paired with the kind of
 * reference-to-existing-text you see in real revision asks:
 *   - bare modify verbs: 改 / 精简 / 压缩 / 扩写 / 润色 / 重写 …
 *   - "把 … 改成/换成 …" targeted replacement
 *   - "换 … 标题" retitling
 * The list stays narrow on purpose (see module doc).
 */
const MODIFY_PREDICATES: RegExp[] = [
  // core single-verb edits (改 must be part of a modify sense, so we also
  // accept it inside the 把…改成 pattern below; standalone 改 with a
  // reference like 改这段/改一下/改简洁 is captured here).
  /改(?:一下|得|成|写|简洁|短|长|口语|正式|通俗|流畅|自然|得?更)/,
  /精简|压缩|扩写|扩充|润色|优化(?:一下|表达)?|重写|改写|调整(?:一下|语气|措辞|结构)?|修改|删掉|删去|去掉/,
  // targeted "把 … 改成/换成 …" within a short span (reference + replacement)
  /把.{0,12}(?:改成|换成|改为|换为)/,
  // retitle: 换.*标题 / 改.*标题
  /(?:换|改|重拟|另拟).{0,6}(?:标题|题目)/,
];

/**
 * Route a chat-box message to the update-canvas path or the normal chat
 * path.
 *
 * @param userMsg        the raw chat-box text the user is about to send
 * @param hasActiveCanvas whether the session currently has an active canvas
 *                        the message could be referring to
 */
export function routeCanvasIntent(userMsg: string, hasActiveCanvas: boolean): CanvasIntent {
  if (!hasActiveCanvas) return 'chat';
  const text = userMsg.trim();
  if (!text) return 'chat';
  // An explicit "write something new" signal always wins — never hijack it
  // into an edit of the current canvas.
  if (NEW_CONTENT_SIGNAL.test(text)) return 'chat';
  const isModify = MODIFY_PREDICATES.some((re) => re.test(text));
  return isModify ? 'update_canvas' : 'chat';
}
