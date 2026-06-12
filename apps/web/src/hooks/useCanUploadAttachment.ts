import { useCallback } from 'react';
import { api } from '../api/client';

// ── hook: pre-upload attachment gate ─────────────────────────────────────────
// Exposes an async `checkFile(file)` that asks the BFF whether the selected
// file may be uploaded BEFORE any upload network call. Large attachments
// (>= the BFF-owned threshold) require the `large-attachment` entitlement;
// small files are always allowed.
//
// The BFF (GET /attachments/large-check) owns the size threshold and the
// entitlement lookup, and FAILS OPEN on any lifecycle failure — so a flaky
// dependency never blocks uploads here. We mirror that: if the check itself
// throws (network error reaching the BFF), we allow, since the server-side
// upload / run-creation path is the real enforcement.

export interface UploadGateResult {
  allowed: boolean;
  reason: string | null;
}

export function useCanUploadAttachment(): {
  checkFile: (file: File) => Promise<UploadGateResult>;
} {
  const checkFile = useCallback(
    async (file: File): Promise<UploadGateResult> => {
      try {
        const res = await api.attachments.largeCheck(file.size);
        return { allowed: res.allowed, reason: res.reason };
      } catch {
        // Fail-open: the gate is a UX pre-check, not the enforcement boundary.
        return { allowed: true, reason: null };
      }
    },
    [],
  );

  return { checkFile };
}
