/**
 * Mock workspace + library data ported from cloud-worker/web/mid/cw-data.jsx.
 *
 * The shapes here are kept intentionally close to the original JSX so that
 * once the backend exposes equivalent endpoints, the page components can
 * swap the imports without restructuring their templates. All user-facing
 * strings have been translated to Chinese.
 */

export interface Workspace {
  id: string;
  name: string;
  glyph: string;
  desc: string;
  accent: string;
}

export interface BaseFile {
  n: string;
  k: string;
  size: string;
  updated: string;
  pinned?: boolean;
  desc?: string;
  children?: BaseFile[];
}

export const WORKSPACES: Workspace[] = [
  { id: 'eng', name: '工程', glyph: '⌥', desc: '代码 · 仓库 · 基础设施', accent: '#d23a1f' },
  { id: 'ops', name: '运营', glyph: '✉', desc: '收件箱 · 财务 · 供应商', accent: '#1f5fd2' },
  { id: 'gtm', name: '市场', glyph: '◐', desc: '演示 · 文案 · 发布', accent: '#0f8a4d' },
  { id: 'cs', name: '客户', glyph: '✦', desc: '工单 · 反馈 · CRM', accent: '#6b3fa0' },
];

export const WORKSPACE_FILES: Record<string, BaseFile[]> = {
  eng: [
    { n: 'coding-conventions.md', k: 'md', size: '4.2 kb', updated: '3 天前', pinned: true, desc: 'TS strict · 禁止 any · 约定式提交' },
    { n: 'repo-map.json', k: 'json', size: '1.8 kb', updated: '1 周前', pinned: true, desc: '12 个仓库 · main + staging 分支' },
    { n: 'deploy-runbook.md', k: 'md', size: '8.1 kb', updated: '2 天前' },
    { n: 'security-checklist.md', k: 'md', size: '3.4 kb', updated: '6 天前' },
    { n: 'on-call.yaml', k: 'yaml', size: '0.6 kb', updated: '12 小时前' },
  ],
  ops: [
    { n: 'monthly-close-checklist.md', k: 'md', size: '6.1 kb', updated: '5 天前', pinned: true },
    { n: 'vendor-contacts.csv', k: 'csv', size: '32 kb', updated: '2 周前', pinned: true },
    { n: 'expense-policy.pdf', k: 'pdf', size: '1.2 mb', updated: '1 个月前' },
  ],
  gtm: [
    { n: 'brand-guidelines.pdf', k: 'pdf', size: '4.4 mb', updated: '2 周前', pinned: true, desc: 'Logo · 字体 · 语气' },
    { n: 'voice-and-tone.md', k: 'md', size: '5.2 kb', updated: '9 天前', pinned: true },
    { n: 'deck-template.pptx', k: 'pptx', size: '1.8 mb', updated: '1 个月前' },
  ],
  cs: [
    { n: 'reply-tone.md', k: 'md', size: '2.2 kb', updated: '2 周前', pinned: true },
    { n: 'macros.json', k: 'json', size: '14 kb', updated: '4 天前' },
  ],
};
