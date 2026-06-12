import { describe, it, expect } from 'vitest';
import { sanitizeStreamingMarkdown } from './sanitizeStreamingMarkdown';

describe('sanitizeStreamingMarkdown', () => {
  it('returns empty string unchanged', () => {
    expect(sanitizeStreamingMarkdown('')).toBe('');
  });

  it('returns plain text unchanged', () => {
    expect(sanitizeStreamingMarkdown('hello world')).toBe('hello world');
  });

  it('passes complete markdown through unchanged', () => {
    const input = '**bold** and *italic* and `code` and ~~strike~~';
    expect(sanitizeStreamingMarkdown(input)).toBe(input);
  });

  it('closes unclosed bold **', () => {
    expect(sanitizeStreamingMarkdown('hello **world')).toBe('hello **world**');
  });

  it('closes unclosed italic *', () => {
    expect(sanitizeStreamingMarkdown('hello *world')).toBe('hello *world*');
  });

  it('closes unclosed fenced code block', () => {
    expect(sanitizeStreamingMarkdown('```\ncode here')).toBe('```\ncode here\n```');
  });

  it('closes unclosed inline backtick', () => {
    expect(sanitizeStreamingMarkdown('run `npm install')).toBe('run `npm install`');
  });

  it('closes unclosed strikethrough ~~', () => {
    expect(sanitizeStreamingMarkdown('~~deleted text')).toBe('~~deleted text~~');
  });

  it('closes unclosed link with partial url', () => {
    expect(sanitizeStreamingMarkdown('[click here](https://example.com')).toBe('[click here](https://example.com)');
  });

  it('strips dangling [ without closing ]', () => {
    expect(sanitizeStreamingMarkdown('some text [dangling')).toBe('some text dangling');
  });
});
