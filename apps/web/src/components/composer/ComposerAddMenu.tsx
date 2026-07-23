import { useEffect, useRef, useState } from 'react';
import type { InstalledSkill, McpServer } from '../../api/types';
import { Chip } from '../shell/Chip';
import { HoverHint } from '../shell/HoverHint';
import { useDropdownState } from '../shell/Dropdown';

// ─── Composer "+" menu ────────────────────────────────────────────────────────
//
// Three sections behind one trigger: 上传文件 / 图片、技能、MCP 工具.
//
// Design: a CLICK-BASED RIGHT-FLYOUT submenu. This replaces an earlier upward
// accordion whose growing panel overflowed the top of the viewport on the home
// composer (which sits in the upper-middle of the screen). The earlier notes
// rejected flyout submenus for three reasons; the new design resolves all of
// them, spelled out below.
//
//  1. COMPACT PRIMARY PANEL + RIGHT FLYOUT, NOT A GROWING ACCORDION. The
//     primary panel is a short 3-row list (📎 文件 / 🧩 技能 / 🔌 MCP 工具)
//     anchored bottom-left of the trigger. It is short enough to never
//     overflow the top. Section content (the skills chip cloud, the MCP server
//     list) lives in a SECONDARY flyout that opens to the RIGHT of the primary
//     panel (`left-full top-0`) with its own max-height + scroll, so a long
//     list scrolls inside the flyout instead of stretching the primary panel.
//  2. ONE OUTSIDE-CLICK OWNER. The primary panel AND its flyout are both
//     rendered inside the single `ref` container that `useDropdownState`
//     watches. A mousedown anywhere outside that container closes the whole
//     menu - there is no second document listener for the flyout, so two
//     outside-click owners can never fight (the original concern #1).
//  3. CLICK, NOT HOVER. Opening a flyout is a click on its primary row, which
//     works under touch - the original concern #2 (hover-intent timers do not).
//  4. ONE FLYOUT AT A TIME. Clicking a row opens its flyout and closes the
//     other; clicking the open row again collapses it. Switching is instant.
//  5. THE FILE ROW IS A LEAF. Clicking it fires `onPickFiles` and closes the
//     whole menu (no flyout), preserving the original one-click upload.
//  6. ESCAPE closes the menu. The handler is scoped to this component's
//     container (a React onKeyDown on the wrapper), not a document-level
//     listener, so it cannot fight the modal Dialog's own Escape - the same
//     reason `useDropdownState` deliberately leaves Escape to callers.
//  7. RIGHT-OVERFLOW FALLBACK. When a flyout opens we measure whether it fits
//     to the right of the primary panel; if not (narrow viewport) we flip it
//     to the LEFT (`right-full`). The measurement is taken once when the
//     flyout opens and does not re-run on live resize (rare while the menu is
//     open) - acceptable, noted here rather than adding a resize listener.
//  8. THE TRIGGER ALWAYS OPENS THE MENU. Both call sites wire all three
//     sections (the home composer parks its pre-session choices in the store's
//     draft slot), so the aria-label is built from whatever IS wired and never
//     over-promises.

export interface ComposerSkillsSection {
  skills: InstalledSkill[];
  /** Enabled skill names; `undefined` = follow the install defaults. */
  selected?: string[];
  onToggle: (name: string) => void;
}

export interface ComposerMcpSection {
  servers: McpServer[];
  /**
   * Enabled server ids for this session. `undefined` = no session-level choice
   * yet, so each row falls back to the server's tenant-level `enabled` flag.
   * An explicit `[]` means "all off" and is NOT the same as `undefined`.
   */
  selected?: string[];
  onToggle: (serverId: string) => void;
}

export interface ComposerPlatformDefaultsSection {
  /** Whether the platform-global default skills are on for this session. */
  enabled: boolean;
  /** Flip the opt-out for this session. */
  onToggle: () => void;
}

interface Props {
  /** Omit to hide the upload row (e.g. uploads not wired). */
  onPickFiles?: () => void;
  uploading?: boolean;
  skills?: ComposerSkillsSection;
  mcp?: ComposerMcpSection;
  /** Omit to hide the platform-defaults toggle (e.g. pre-session composer). */
  platformDefaults?: ComposerPlatformDefaultsSection;
  disabled?: boolean;
}

type SectionId = 'skills' | 'mcp';

/** Whether an MCP server is on for this session (tenant default until chosen). */
export function isMcpServerOn(server: McpServer, selected?: string[]): boolean {
  return selected === undefined ? server.enabled : selected.includes(server.id);
}

/** Whether a skill is on (install default until the session/draft chooses). */
export function isSkillOn(skill: InstalledSkill, selected?: string[]): boolean {
  return selected === undefined ? skill.enabled_by_default : selected.includes(skill.name);
}

/** Short "工具：a、b、c" summary from the last connectivity test. */
function toolSummary(server: McpServer): string {
  const tools = server.discovered_tools ?? [];
  if (tools.length === 0) return '尚未探测到工具';
  return `工具：${tools.map((t) => t.name).join('、')}`;
}

export function ComposerAddMenu({
  onPickFiles,
  uploading,
  skills,
  mcp,
  platformDefaults,
  disabled,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDropdownState(ref);
  const hasSkills = !!skills;
  const hasMcp = !!mcp;
  // No flyout is open until the user clicks a section row, so the primary
  // panel stays a compact 3 rows no matter how long the content is.
  const [expanded, setExpanded] = useState<SectionId | null>(null);

  // Reset the open flyout whenever the whole menu closes (by outside click,
  // Escape, the file row, or toggling the trigger) so the next open starts
  // clean. `useDropdownState` closes on outside mousedown directly, so this
  // effect is the single place that catches every close path.
  useEffect(() => {
    if (!open) setExpanded(null);
  }, [open]);

  // Right-overflow fallback (note 7): when a flyout opens, measure whether it
  // fits to the right of the primary panel; if not, flip it to the left.
  const [flipLeft, setFlipLeft] = useState(false);
  useEffect(() => {
    if (!open || expanded === null) {
      setFlipLeft(false);
      return;
    }
    const outer = ref.current;
    if (!outer) return;
    const left = outer.getBoundingClientRect().left;
    const PRIMARY_W = 176; // w-44
    const SECONDARY_W = 288; // w-72
    const GAP = 4;
    const rightSpace = window.innerWidth - left - PRIMARY_W;
    setFlipLeft(rightSpace < SECONDARY_W + GAP);
  }, [open, expanded]);

  if (!onPickFiles && !hasSkills && !hasMcp && !platformDefaults) return null;

  // Name the trigger after the sections that are actually wired, e.g.
  // 「添加文件、技能或工具」 with all three, 「添加文件」 with uploads alone.
  const parts = [onPickFiles ? '文件' : null, hasSkills ? '技能' : null, hasMcp ? '工具' : null]
    .filter((p): p is string => p !== null);
  const label =
    '添加' +
    (parts.length > 1 ? `${parts.slice(0, -1).join('、')}或${parts[parts.length - 1]}` : parts[0]);

  const skillCount = skills ? skills.skills.filter((s) => isSkillOn(s, skills.selected)).length : 0;
  const mcpOnCount = mcp ? mcp.servers.filter((s) => isMcpServerOn(s, mcp.selected)).length : 0;

  const toggleSection = (id: SectionId) => setExpanded((cur) => (cur === id ? null : id));
  const flyoutPlacement = flipLeft ? 'right-full top-0 mr-1' : 'left-full top-0 ml-1';

  return (
    <div
      ref={ref}
      className="relative shrink-0"
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.preventDefault();
          setOpen(false);
        }
      }}
    >
      <HoverHint label={label} align="left">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          disabled={disabled || uploading}
          className="w-9 h-9 shrink-0 inline-flex items-center justify-center rounded-full bg-paper text-ink text-[21px] leading-none hover:bg-paper-2 transition-colors disabled:opacity-40"
          aria-label={label}
          aria-haspopup="menu"
          aria-expanded={open}
        >
          <span aria-hidden className="font-sans -translate-y-px">+</span>
        </button>
      </HoverHint>

      {open && (
        <div
          role="menu"
          aria-label={label}
          className="absolute bottom-full left-0 mb-2 w-44 z-20 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1.5 animate-pop-in origin-bottom"
        >
          {/* ── 上传文件 / 图片 (leaf: no flyout) ── */}
          {onPickFiles && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                onPickFiles();
                setOpen(false);
              }}
              disabled={uploading}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md font-mono text-[12px] text-ink-2 hover:bg-paper disabled:opacity-40 text-left"
            >
              <span aria-hidden>📎</span>
              <span className="flex-1">上传文件 / 图片</span>
            </button>
          )}

          {/* ── 技能 (flyout) ── */}
          {skills && (
            <button
              type="button"
              role="menuitem"
              aria-haspopup="true"
              aria-expanded={expanded === 'skills'}
              aria-controls={expanded === 'skills' ? 'composer-add-menu-skills' : undefined}
              onClick={() => toggleSection('skills')}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md font-mono text-[12px] text-ink-2 hover:bg-paper text-left"
            >
              <span aria-hidden>🧩</span>
              <span className="flex-1">技能</span>
              <span className="font-mono text-[11px] text-dim">
                {skillCount}/{skills.skills.length}
              </span>
              <span aria-hidden className="text-[8px] text-dim">
                ▸
              </span>
            </button>
          )}

          {/* ── MCP 工具 (flyout) ── */}
          {mcp && (
            <button
              type="button"
              role="menuitem"
              aria-haspopup="true"
              aria-expanded={expanded === 'mcp'}
              aria-controls={expanded === 'mcp' ? 'composer-add-menu-mcp' : undefined}
              onClick={() => toggleSection('mcp')}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md font-mono text-[12px] text-ink-2 hover:bg-paper text-left"
            >
              <span aria-hidden>🔌</span>
              <span className="flex-1">MCP 工具</span>
              <span className="font-mono text-[11px] text-dim">
                {mcpOnCount}/{mcp.servers.length}
              </span>
              <span aria-hidden className="text-[8px] text-dim">
                ▸
              </span>
            </button>
          )}

          {/* ── 平台默认技能 (leaf toggle: no flyout, keeps menu open) ── */}
          {platformDefaults && (
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={platformDefaults.enabled}
              title="平台为每个组织内置的默认技能。关闭后本次会话不再自动注入它们。"
              onClick={() => platformDefaults.onToggle()}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md font-mono text-[12px] text-ink-2 hover:bg-paper text-left"
            >
              <span aria-hidden>🌐</span>
              <span className="flex-1">平台默认技能</span>
              <span
                aria-hidden
                className={`shrink-0 w-3.5 h-3.5 inline-flex items-center justify-center rounded-[3px] border text-[9px] leading-none ${
                  platformDefaults.enabled
                    ? 'border-accent-line bg-accent-soft text-accent'
                    : 'border-line text-transparent'
                }`}
              >
                ✓
              </span>
            </button>
          )}

          {/* ── Skills flyout (right of primary, own scroll) ── */}
          {skills && expanded === 'skills' && (
            <div
              id="composer-add-menu-skills"
              role="group"
              aria-label="技能"
              className={`absolute ${flyoutPlacement} w-72 max-h-[60vh] overflow-y-auto z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-2 animate-pop-in ${flipLeft ? 'origin-right' : 'origin-left'}`}
            >
              <div className="flex flex-wrap gap-1">
                {skills.skills.map((s) => {
                  const on = isSkillOn(s, skills.selected);
                  return (
                    <Chip
                      key={s.id}
                      accent={on}
                      dashed={!on}
                      onClick={() => skills.onToggle(s.name)}
                    >
                      {s.name}
                    </Chip>
                  );
                })}
                {skills.skills.length === 0 && (
                  <span className="font-mono text-[11px] italic text-dim">暂无 skill</span>
                )}
              </div>
            </div>
          )}

          {/* ── MCP flyout (right of primary, own scroll) ── */}
          {mcp && expanded === 'mcp' && (
            <div
              id="composer-add-menu-mcp"
              role="group"
              aria-label="MCP 工具"
              className={`absolute ${flyoutPlacement} w-72 max-h-[60vh] overflow-y-auto z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1.5 animate-pop-in ${flipLeft ? 'origin-right' : 'origin-left'}`}
            >
              <div className="flex flex-col gap-0.5">
                {mcp.servers.map((server) => {
                  const on = isMcpServerOn(server, mcp.selected);
                  const summary = toolSummary(server);
                  return (
                    <button
                      key={server.id}
                      type="button"
                      role="menuitemcheckbox"
                      aria-checked={on}
                      title={summary}
                      onClick={() => mcp.onToggle(server.id)}
                      className="w-full flex items-start gap-2 px-2 py-1.5 rounded-md text-left hover:bg-paper"
                    >
                      <span
                        aria-hidden
                        className={`mt-px shrink-0 w-3.5 h-3.5 inline-flex items-center justify-center rounded-[3px] border text-[9px] leading-none ${
                          on
                            ? 'border-accent-line bg-accent-soft text-accent'
                            : 'border-line text-transparent'
                        }`}
                      >
                        ✓
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[12px] text-ink">{server.name}</span>
                        <span className="block truncate font-mono text-[10.5px] text-dim">
                          {summary}
                        </span>
                      </span>
                    </button>
                  );
                })}
                {mcp.servers.length === 0 && (
                  <span className="px-2 font-mono text-[11px] italic text-dim">
                    暂无 MCP 工具
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
