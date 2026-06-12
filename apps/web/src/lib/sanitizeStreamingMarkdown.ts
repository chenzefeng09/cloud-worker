/**
 * Close trailing unclosed markdown delimiters so ReactMarkdown doesn't mangle
 * partial syntax during streaming.  Only called for actively-streaming content.
 */
export function sanitizeStreamingMarkdown(text: string): string {
  let s = text;

  // Close unclosed fenced code blocks
  const fences = s.match(/^```/gm) || [];
  if (fences.length % 2 !== 0) {
    s += '\n```';
  }

  // Close unclosed inline code — count backticks outside fenced blocks
  const withoutFences = s.replace(/```[\s\S]*?```/g, '');
  if ((withoutFences.match(/`/g) || []).length % 2 !== 0) {
    s += '`';
  }

  // Close unclosed bold ** — count outside of code spans
  const withoutCode = withoutFences.replace(/`[^`]*`/g, '');
  const boldMarkers = withoutCode.match(/\*\*/g) || [];
  if (boldMarkers.length % 2 !== 0) {
    s += '**';
  }

  // Close unclosed italic * (single, not part of **)
  const withoutBold = withoutCode.replace(/\*\*/g, '');
  const italicMarkers = withoutBold.match(/\*/g) || [];
  if (italicMarkers.length % 2 !== 0) {
    s += '*';
  }

  // Close unclosed strikethrough ~~
  const strikeMarkers = withoutCode.match(/~~/g) || [];
  if (strikeMarkers.length % 2 !== 0) {
    s += '~~';
  }

  // Close unclosed link/image: [text](url  without closing )
  if (/\[[^\]]*\]\([^)]*$/.test(s)) {
    s += ')';
  }

  // Remove unclosed [ that would eat following text
  const lastBracket = s.lastIndexOf('[');
  if (lastBracket >= 0 && s.indexOf(']', lastBracket) < 0) {
    s = s.slice(0, lastBracket) + s.slice(lastBracket + 1);
  }

  return s;
}
