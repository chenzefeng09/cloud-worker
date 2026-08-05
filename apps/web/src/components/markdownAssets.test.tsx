/**
 * Markdown image resolution — the fix for broken `![x](outputs/chart.png)`
 * images in agent output. The model writes sandbox-relative paths; the browser
 * would resolve them against the page URL and 404. We map them onto the real
 * attachment content URL by filename, and never leave a broken image behind.
 */
import { render, screen } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';
import { describe, expect, it, vi } from 'vitest';
import { markdownComponents } from './chatStepRenderers';
import {
  MarkdownAssetsProvider,
  assetBasename,
  makeAttachmentImageResolver,
  type MarkdownAssets,
} from './markdownAssets';

const SID = 'ses_abc';
const ATTS = [
  { id: 'att_01JQ7Z', filename: 'sales_trend.png', mime: 'image/png' },
  { id: 'att_02CJKX', filename: '月度趋势.png', mime: 'image/png' },
];

function renderMd(md: string, assets?: Partial<MarkdownAssets>) {
  const value: MarkdownAssets = {
    resolveImage: makeAttachmentImageResolver(ATTS, SID),
    ...assets,
  };
  return render(
    <MarkdownAssetsProvider value={value}>
      <ReactMarkdown components={markdownComponents}>{md}</ReactMarkdown>
    </MarkdownAssetsProvider>,
  );
}

describe('assetBasename', () => {
  it('strips sandbox path prefixes, query/hash tails and percent-encoding', () => {
    expect(assetBasename('outputs/sales_trend.png')).toBe('sales_trend.png');
    expect(assetBasename('./sales_trend.png')).toBe('sales_trend.png');
    expect(assetBasename('/workspace/outputs/sales_trend.png')).toBe('sales_trend.png');
    expect(assetBasename('outputs/chart.png?v=2#frag')).toBe('chart.png');
    expect(assetBasename('outputs/%E6%9C%88%E5%BA%A6%E8%B6%8B%E5%8A%BF.png')).toBe('月度趋势.png');
    // a malformed escape must not throw — the raw segment is the fallback
    expect(assetBasename('outputs/%E4%bad.png')).toBe('%E4%bad.png');
  });
});

describe('markdown image rendering', () => {
  it('resolves a sandbox path to the real attachment content URL', () => {
    const { container } = renderMd('![月度销量与金额趋势](outputs/sales_trend.png)');
    const img = container.querySelector('img');
    expect(img).not.toBeNull();
    expect(img!.getAttribute('src')).toBe(
      `/api/bff/v1/sessions/${SID}/files/att_01JQ7Z/content`,
    );
    expect(img!.getAttribute('alt')).toBe('月度销量与金额趋势');
  });

  it('matches CJK filenames and is case-insensitive on the basename', () => {
    const { container } = renderMd('![趋势](outputs/月度趋势.png)');
    expect(container.querySelector('img')!.getAttribute('src')).toBe(
      `/api/bff/v1/sessions/${SID}/files/att_02CJKX/content`,
    );

    const upper = renderMd('![x](outputs/SALES_TREND.PNG)');
    expect(upper.container.querySelector('img')!.getAttribute('src')).toBe(
      `/api/bff/v1/sessions/${SID}/files/att_01JQ7Z/content`,
    );
  });

  it('renders a labelled placeholder — and NO <img> — when the file is unknown', () => {
    const { container } = renderMd('![x](outputs/missing_chart.png)');
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(/图片未找到：missing_chart\.png/)).toBeTruthy();
  });

  it('leaves absolute http(s) sources untouched', () => {
    for (const src of ['https://example.com/a.png', 'http://example.com/b.png']) {
      const { container, unmount } = renderMd(`![x](${src})`);
      expect(container.querySelector('img')!.getAttribute('src')).toBe(src);
      unmount();
    }
  });

  it('passes data:/blob: straight through when they reach the renderer', () => {
    // react-markdown's default urlTransform strips these before we ever see
    // them (we deliberately do NOT loosen it), so exercise the branch directly.
    const Img = markdownComponents!.img as React.ComponentType<{ src?: string; alt?: string }>;
    for (const src of ['data:image/png;base64,iVBORw0KGgo=', 'blob:http://localhost/9f1']) {
      const { container, unmount } = render(<Img src={src} alt="x" />);
      expect(container.querySelector('img')!.getAttribute('src')).toBe(src);
      unmount();
    }
  });

  it('renders nothing (not a broken image) when the sanitiser blanks the src', () => {
    const { container } = renderMd('![x](data:image/png;base64,iVBORw0KGgo=)');
    expect(container.querySelector('img')).toBeNull();
  });

  it('click opens the page preview panel when a handler is provided', async () => {
    const onOpen = vi.fn();
    renderMd('![图](outputs/sales_trend.png)', { onOpen });
    screen.getByRole('button', { name: '查看 sales_trend.png' }).click();
    expect(onOpen).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'att_01JQ7Z', filename: 'sales_trend.png' }),
    );
  });

  it('falls back to the placeholder with no provider at all', () => {
    const { container } = render(
      <ReactMarkdown components={markdownComponents}>
        {'![x](outputs/sales_trend.png)'}
      </ReactMarkdown>,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(/图片未找到：sales_trend\.png/)).toBeTruthy();
  });

  it('resolver yields null without a session (no URL can be built)', () => {
    expect(makeAttachmentImageResolver(ATTS, undefined)('outputs/sales_trend.png')).toBeNull();
  });
});
