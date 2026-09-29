# Role runtime v1 candidate

This is a proposed contract, not a root `contracts/` publication. Platform owns the
web application intent, provider revision, source registration, and bot binding.
Companion owns the role's persona revision and turn snapshot. Memory owns the
exact actor grant and scoped memory. All remote calls use the existing distinct
HTTPS service credentials; a browser session never calls a peer directly.

`POST /api/web/roles/apply` takes `platform_apply`. `client_id` identifies one
immutable application intent; replay with different bytes is a conflict. The
response carries a `platform_role`. `pending` never counts as active. Platform
performs `core_apply(enabled=false)`, `memory_apply`, checks the exact provider
revision, then `core_apply(enabled=true)` and marks the intent active. A peer
response loss is recovered with the same stage request ID. On restart an active
role is pending until Core and Memory answer with the recorded versions.

`POST /internal/v1/role-runtime/manage` on Companion accepts `operation:list`
or `operation:apply` plus `core_apply`. Only its registered Platform caller can
write. `POST /internal/v1/role-runtime/authorize` on Memory accepts `status`
or `memory_apply`; only the configured Platform `role_admin` caller can write.
Each write uses `expected_version` and an idempotency key. An enabled grant is
only usable by a caller explicitly configured with `allow_runtime_roles:true`;
the origin and exact actor scope checks still apply.

For a production rollout, quiesce writes and take a consistent backup of the
Platform base DB plus `<database_path>.roles.sqlite`, the complete provider
catalog directory including its key, the Companion DB, and the Memory DB plus
`role_grants_database_path`. Restore these as one set. Provision unique service
tokens, trusted CA paths and the absolute grant path before enabling the page.
Existing deployment roles appear as `legacy_roles` in the view. An operator may
explicitly adopt one with its current actor ID and `profile_id/profile_version`
both null. This retains its published persona and static source/BOT binding;
the selected provider becomes role-specific. Adoption is not automatic and no
production legacy role is changed by this task. A managed disable overrides
the old static role/grant while retaining its history and configuration.

The joint test uses real HTTPS owners and the read-only Gateway baseline. Its
isolated gateway fixture allows only 127.0.0.1 as a synthetic recorded model
target; production Gateway target policy remains unchanged.
