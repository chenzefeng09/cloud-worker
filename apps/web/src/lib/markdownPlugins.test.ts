import { describe, it, expect } from 'vitest';
import { normalizeMathDelimiters } from './markdownPlugins';

describe('normalizeMathDelimiters', () => {
  it('converts display math written with LaTeX bracket delimiters', () => {
    const src = '动力学方程：\n\n\\[\nm \\ddot{x} + k x = f(t)\n\\]\n';
    expect(normalizeMathDelimiters(src)).toBe(
      '动力学方程：\n\n$$\nm \\ddot{x} + k x = f(t)\n$$\n',
    );
  });

  it('converts inline math written with \\( \\)', () => {
    expect(normalizeMathDelimiters('其中 \\(u(t)\\) 为阶跃输入')).toBe(
      '其中 $u(t)$ 为阶跃输入',
    );
  });

  it('leaves dollar-delimited math untouched', () => {
    const src = '超调量 $M_p$ 与 $$\\zeta$$ 有关';
    expect(normalizeMathDelimiters(src)).toBe(src);
  });

  it('does not rewrite delimiters inside fenced code', () => {
    const src = '```bash\nsed "s/\\\\(a\\\\)/b/" f.txt\n```\n';
    expect(normalizeMathDelimiters(src)).toBe(src);
  });

  it('does not rewrite delimiters inside inline code', () => {
    const src = '正则 `\\(group\\)` 不是公式';
    expect(normalizeMathDelimiters(src)).toBe(src);
  });

  it('passes through markdown with no math at all', () => {
    const src = '# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n';
    expect(normalizeMathDelimiters(src)).toBe(src);
  });
});
