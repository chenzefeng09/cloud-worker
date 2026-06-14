/**
 * Frontend permission gating utilities.
 *
 * These mirror the authz service's dotted-wildcard `covers` semantics so the
 * admin console can gate modules client-side without re-deriving rules:
 *   - `*`            covers everything
 *   - `xxx.*`        covers any `xxx.<...>` token (but NOT a bare `xxx`)
 *   - exact match    covers itself
 *
 * The source of truth is still the backend (every privileged call is
 * re-checked server-side); this only governs what we render / route to.
 */

/** Whether the held permission token `holder` covers the `required` token. */
export function permissionCovers(holder: string, required: string): boolean {
  if (holder === '*') return true;
  if (holder === required) return true;
  if (holder.endsWith('.*')) {
    const prefix = holder.slice(0, -2).split('.');
    const reqCore = required.endsWith('.*') ? required.slice(0, -2) : required;
    if (!reqCore.includes('.')) return false; // a bare single segment is not covered by xxx.*
    return reqCore.split('.').slice(0, prefix.length).join('.') === prefix.join('.');
  }
  return false;
}

/** Whether a user's permission set satisfies a single `required` permission. */
export function hasPermission(perms: string[] | undefined, required: string): boolean {
  if (!perms) return false;
  return perms.some((p) => permissionCovers(p, required));
}

/** Whether the user is a platform-level super admin (may act across tenants). */
export function isPlatformAdmin(perms: string[] | undefined): boolean {
  return hasPermission(perms, 'platform.admin') || (perms?.includes('*') ?? false);
}
