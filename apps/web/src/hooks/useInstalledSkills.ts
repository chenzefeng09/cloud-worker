import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { InstalledSkill } from '../api/types';

// ── hook: installed skills ───────────────────────────────────────────────────
// Shows the user's installed skills (their toolbox), not the browse shelf.
// enabled_by_default seeds the per-session selection (see ChatWindow).
export function useInstalledSkills() {
  const [skills, setSkills] = useState<InstalledSkill[]>([]);

  useEffect(() => {
    api.library.listInstalledSkills()
      .then((res) => setSkills(Array.isArray(res?.items) ? res.items : []))
      .catch(() => {/* failure-open */});
  }, []);

  return skills;
}
