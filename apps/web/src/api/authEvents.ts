/**
 * "The session may have ended" signal, shared by the API layer and AuthGate.
 *
 * The BFF keeps a browser session alive by refreshing its tokens server-side,
 * so a 401 from it means the session could not be kept: the cookie expired,
 * the refresh was refused (signed out elsewhere, password changed, idle or
 * absolute timeout) or the session was revoked.  API helpers report such a
 * 401 with `notifySessionExpired()`; `AuthGate` confirms it against
 * `/auth/me` and sends the user back to the sign-in page.
 *
 * This lives outside `client.ts` so tests that mock the client module
 * wholesale do not have to stub it.
 */

type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe to session-expiry reports; returns the unsubscribe function. */
export function onSessionExpired(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Report a 401 that suggests the browser session is no longer valid. */
export function notifySessionExpired(): void {
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch (err) {
      console.warn('session-expired listener failed', err);
    }
  }
}
