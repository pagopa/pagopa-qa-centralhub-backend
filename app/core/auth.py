from __future__ import annotations

from app.config import settings


async def verify_jwt(token: str) -> dict | None:
    """Validate a JWT issued by the configured OIDC provider.

    TODO: implement using Authlib's AsyncJWTClaims or jose to verify
    signature against the OIDC provider's JWKS endpoint and extract
    the role claim.
    """
    raise NotImplementedError("JWT verification not yet implemented")
