# Persona selector labels

Display only the profile name in the role configuration selector. The internal
profile record version tracks concurrent edits and application metadata; it is not
a content edition. Option IDs and binding version/revision handling are unchanged.
No history deletion, backend, schema, permission or credential changes.

Validation: TypeScript and production build passed; real TLS owners plus Chromium
passed select/save/reopen/repeated save/clear/no-profile scenarios with exact name
labels and stable content revision counts. Companion's nine role runtime tests
passed, including repeated pause/enable saves, changed profile and legacy persona
preservation. Runtime coordination versions continue to advance as designed.
