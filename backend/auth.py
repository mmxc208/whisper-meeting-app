"""Authentication for requests forwarded by the App Kit reverse proxy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Mapping

from kiro_crew.apps.proxy_auth import (
    PRINCIPAL_OWNER_SESSION,
    ProxyPrincipalReplayCache,
    verify_proxy_principal_claim,
    verify_proxy_request,
)


@dataclass(frozen=True)
class RequestIdentity:
    """The verified owner identity carried by one proxied request."""

    owner_id: str


class ProxyAuthenticator:
    """Verify App Kit request and principal signatures.

    A replay cache belongs to the backend process rather than to an individual
    request. This prevents a valid owner claim from being replayed during its
    freshness window.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._principal_replays = ProxyPrincipalReplayCache()
        self._clock = clock

    def verify(
        self,
        *,
        method: str,
        target: str,
        body: bytes,
        headers: Mapping[str, str],
        require_owner: bool,
    ) -> RequestIdentity | None:
        proxy_header = headers.get("X-KiroCrew-Proxy", "")
        if not verify_proxy_request(
            proxy_header,
            method=method,
            target=target,
            body=body,
            now=None if self._clock is None else self._clock(),
        ):
            return None

        if not require_owner:
            return RequestIdentity(owner_id="")

        claim = verify_proxy_principal_claim(
            headers.get("X-KiroCrew-Principal", ""),
            method=method,
            target=target,
            body=body,
            replay_cache=self._principal_replays,
            now=None if self._clock is None else self._clock(),
        )
        if claim is None or claim.kind != PRINCIPAL_OWNER_SESSION:
            return None
        return RequestIdentity(owner_id=claim.owner_id)
