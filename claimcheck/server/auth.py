"""Bearer-token check for the investigations API and the dashboard.

The token comes from CLAIMCHECK_API_TOKEN. The webhook is not covered: it has its own HMAC
signature, and GitHub cannot send a bearer token. With no token configured nothing is checked,
and `python -m claimcheck.server` then refuses to bind anything but a loopback host.
"""

import hmac

from starlette.requests import Request

API_PREFIX = "/v1/investigations"
DASHBOARD_PREFIX = "/dashboard"


def protected(path: str) -> str | None:
    """'api' or 'dashboard' for paths that need the token, None for anything else."""
    if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
        return "api"
    if path == "/" or path == DASHBOARD_PREFIX or path.startswith(DASHBOARD_PREFIX + "/"):
        return "dashboard"
    return None


def authorized(request: Request, token: str) -> bool:
    """True when the request carries `Authorization: Bearer <token>`. Compared as bytes with
    `hmac.compare_digest`, so the time taken does not reveal how much of a guess was right."""
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    # Starlette decodes header values as latin-1, so this gives back the bytes that were sent.
    return hmac.compare_digest(credentials.strip().encode("latin-1"), token.encode("utf-8"))
