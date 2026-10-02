# Persona authoring over LAN HTTP

Persona create/save/apply failed before fetch on non-secure HTTP origins because
`crypto.randomUUID` is unavailable there. Reuse the existing cryptographic UUIDv4
`requestId()` helper, preserving the existing request identity across retries.
No API, permissions, credentials, role content, backend or schema changes.

Validation: TypeScript and production build passed. The offline browser regression
`node tests/backend/persona_http_browser.cjs` uses the real built UI at a non-secure
HTTP origin and a synthetic API: 10,320-byte multiline content, create with lost
response, same-ID retry, one stored profile, reopen/save, and unchanged legacy role.
Real NAS isolated backend acceptance and production resource readback are deployment
gates; this source handoff does not claim those future steps have run.
