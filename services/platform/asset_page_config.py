"""Deployment-time rule for the read-only asset page's own configuration.

This module exists to keep one dependency direction honest. `web_assets` (the page adapter) needs
`page_configuration`; `assets` (the application client that owns the five published reads) needs the
same function to validate its `web_assets` section at deployment time. If the rule lived in the page
module, the client would have to import the page, which is the wrong way round: a client must not
depend on one of its consumers.

So the rule lives here, in a neutral module that knows only about principals, connections and the
contract check it is handed — no HTTP, no session, no page, no adapter. Both sides import this
module, and neither imports the other.

The rule itself is unchanged from where it was: it narrows and never grants. The page can only offer
a connection the registered identity is already bound to *and* the deployment already declares.
"""

from .contracts import require

PAGE_SETTING_KEYS = {"enabled", "principal", "allowed_connections"}
PAGE_CONNECTION_LIMIT = 16


def page_configuration(section, principals, connections, check):
    """Validate the `web_assets` section and return `(enabled, principal, allowed connections)`.

    `principals` is the deployment's registered identity table, `connections` the deployment's
    registered asset connections, and `check` the contract checker used for identifier shape.
    """
    require(
        isinstance(section, dict)
        and set(section) <= PAGE_SETTING_KEYS
        and {"principal", "allowed_connections"} <= set(section),
        "invalid_input",
        400,
    )
    require(type(section.get("enabled", False)) is bool, "invalid_input", 400)
    principal_name = section["principal"]
    require(isinstance(principal_name, str) and principal_name in principals, "invalid_input", 400)
    principal = principals[principal_name]
    # This page never reads assets as an operator's browser session and never as the console's own
    # source identity: only a registered service identity with an explicit `asset.read`.
    require(
        principal.get("kind") == "service"
        and principal.get("service") == "platform"
        and "asset.read" in set(principal.get("actions", [])),
        "invalid_input",
        400,
    )
    allowed = section["allowed_connections"]
    require(
        isinstance(allowed, list)
        and 1 <= len(allowed) <= PAGE_CONNECTION_LIMIT
        and len(set(allowed)) == len(allowed),
        "invalid_input",
        400,
    )
    bound = set(principal.get("asset_connections", []))
    for connection_id in allowed:
        check("common#id", connection_id)
        # An undeclared connection is not a connection at all, and a connection this identity is
        # not bound to is never granted here.
        require(connection_id in connections, "invalid_input", 400)
        require(connection_id in bound, "invalid_input", 400)
    return section.get("enabled", False), principal_name, tuple(allowed)
