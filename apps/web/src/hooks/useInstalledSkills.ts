import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { InstalledSkill } from '../api/types';

// ── hook: installed skills ───────────────────────────────────────────────────
// Shows the user's installed skills (their toolbox), not the browse shelf.
// enabled_by_default seeds the per-session selection (see ChatWindow).
//
// Returns the array as-is (so existing callers like ChatWindow keep working
// with `.length` / `.filter` / `.map`), with a `refresh` function tacked on
// for pages that need to re-poll after install/uninstall (LibraryPage).
export type InstalledSkillsResult = InstalledSkill[] & { refresh: () => void };

export function useInstalledSkills(): InstalledSkillsResult {
  const [skills, setSkills] = useState<InstalledSkill[]>([]);

  const refresh = useCallback(() => {
    api.library.listInstalledSkills()
      .then((res) => setSkills(Array.isArray(res?.items) ? res.items : []))
      .catch(() => {/* failure-open */});
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Tack the refresh onto the array. ChatWindow ignores the extra field; the
  // LibraryPage uses it to re-pull after a successful install.
  const out = skills as InstalledSkillsResult;
  out.refresh = refresh;
  return out;
}
