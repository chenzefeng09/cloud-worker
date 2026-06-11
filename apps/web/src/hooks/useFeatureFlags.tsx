import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import type { FeatureFlag } from '../api/types';
import { useChatStore } from '../stores/chatStore';

type FlagMap = Map<string, boolean>;

const FeatureFlagContext = createContext<FlagMap | null>(null);

function buildMap(flags: FeatureFlag[]): FlagMap {
  const m = new Map<string, boolean>();
  for (const f of flags) {
    m.set(f.key, !!f.enabled && !f.kill_switch_active);
  }
  return m;
}

export function FeatureFlagProvider({ children }: { children: ReactNode }) {
  const userProfile = useChatStore((s) => s.userProfile);
  const [flags, setFlags] = useState<FlagMap | null>(null);

  useEffect(() => {
    if (!userProfile) {
      setFlags(null);
      return;
    }
    let cancelled = false;
    api.featureFlags
      .list()
      .then((data) => { if (!cancelled) setFlags(buildMap(data)); })
      .catch(() => { if (!cancelled) setFlags(new Map()); });
    return () => { cancelled = true; };
  }, [userProfile]);

  return (
    <FeatureFlagContext.Provider value={flags}>
      {children}
    </FeatureFlagContext.Provider>
  );
}

export function useFeatureFlag(key: string): boolean {
  const flags = useContext(FeatureFlagContext);
  return flags?.get(key) ?? false;
}

export function useFeatureFlags(): FlagMap {
  const flags = useContext(FeatureFlagContext);
  return flags ?? new Map();
}
