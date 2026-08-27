const PROVIDER_KEY = /\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}/i;
const BEARER_CREDENTIAL = /\bBearer\s+[A-Za-z0-9._~+/-]{16,}/i;
// Match an actual header line or serialized header field. A legitimate audit
// identity such as `service.authorization:<uuid>` must remain admissible.
const AUTHORIZATION_HEADER = /(?:^|[\r\n])\s*Authorization\s*:|["']Authorization["']\s*:/i;

export function containsSecretShape(value: string): boolean {
  return PROVIDER_KEY.test(value)
    || BEARER_CREDENTIAL.test(value)
    || AUTHORIZATION_HEADER.test(value);
}
