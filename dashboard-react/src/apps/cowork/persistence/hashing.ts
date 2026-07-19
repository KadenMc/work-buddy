/**
 * Lowercase hex SHA-256 of an opaque byte blob (the hash shape the doc routes use for
 * `X-WB-Doc-Sha256` and content-addressed snapshot blobs). Uses Web Crypto, which is
 * available in both the browser dashboard runtime and the test environment.
 */
export const sha256Hex = async (bytes: Uint8Array): Promise<string> => {
  const source = new Uint8Array(bytes);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", source);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
};
