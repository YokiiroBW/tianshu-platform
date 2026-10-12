"""Provider value objects and connection syntax, shared by catalogue owners."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from .contracts import Fault, require

PROTOCOL = "openai-chat-completions"


@dataclass(frozen=True)
class ProviderExecutionContext:
    """Ephemeral private DTO; repr excludes endpoint/model and credential values."""

    provider_id: str
    revision: int
    protocol: str
    base_url: str = field(repr=False)
    model_id: str = field(repr=False)
    api_key: str = field(repr=False)
    kind: str = "text"


def text(value, maximum):
    require(isinstance(value, str), "invalid_input", 400)
    value = value.strip()
    require(0 < len(value) <= maximum and not any(ord(c) < 32 for c in value), "invalid_input", 400)
    return value


def normalize_base_url(value):
    """Syntax only. Executor still has to enforce DNS, TLS, target and redirect policy."""
    value = text(value, 2048)
    require(
        not any(ord(c) <= 32 or ord(c) > 126 for c in value)
        and "\\" not in value
        and "%" not in value,
        "invalid_input",
        400,
    )
    try:
        url = urlsplit(value)
        require(
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment,
            "invalid_input",
            400,
        )
        port = url.port
        require(port is None or 0 < port <= 65535, "invalid_input", 400)
        host = url.hostname.lower()
        require(not any(part in {".", ".."} for part in url.path.split("/")), "invalid_input", 400)
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != 443:
            authority += f":{port}"
        return urlunsplit(("https", authority, url.path.rstrip("/"), "", ""))
    except ValueError:
        raise Fault("invalid_input", 400) from None
