// Compute the hex-encoded SHA-256 of a File's bytes using the Web Crypto API.
//
// `crypto.subtle` is only available in secure contexts (https or localhost).
// Dev runs on http://localhost:5173 so it is available there, but to stay safe
// in any other context we return `null` instead of throwing when the digest
// cannot be computed — callers should treat the hash as optional and skip
// `expected_sha256` rather than blocking the upload.
export async function sha256Hex(file: File): Promise<string | null> {
  try {
    if (!globalThis.crypto?.subtle) return null;
    const buf = await file.arrayBuffer();
    const digest = await globalThis.crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  } catch {
    return null;
  }
}
