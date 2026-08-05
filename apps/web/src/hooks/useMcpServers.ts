import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import type { McpServer } from '../api/types';

// ── hook: tenant MCP servers ─────────────────────────────────────────────────
// Backs the composer "+" menu's 「MCP 工具」section. The endpoint returns a
// ListPage wrapper ({items,total}), NOT a bare array — unwrap it here so every
// consumer gets a plain array.
//
// `enabled` on a server is the TENANT default (what a session that never
// expressed a choice gets); the per-session override lives in
// chatStore.selectedMcpServerIds. `discovered_tools` is whatever the last
// connectivity test found — displayed as-is, we never probe from the composer.
export interface McpServersResult {
  servers: McpServer[];
  refresh: () => void;
}

export function useMcpServers(): McpServersResult {
  const [servers, setServers] = useState<McpServer[]>([]);

  const refresh = useCallback(() => {
    api.library
      .listMcpServers()
      .then((res) => setServers(Array.isArray(res?.items) ? res.items : []))
      .catch(() => {
        /* failure-open: no MCP section rather than a broken composer */
      });
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { servers, refresh };
}
