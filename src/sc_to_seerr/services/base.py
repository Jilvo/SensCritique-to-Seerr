"""Client HTTP asynchrone commun aux services, avec nouvelles tentatives."""

import asyncio
import logging
from typing import Any, Self

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ServiceError(RuntimeError):
    """Erreur renvoyée par un service distant."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BaseService:
    name = "service"

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._retries = retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": "sc-to-seerr/0.1", **(headers or {})},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, *, retry: bool = True, **kwargs: Any) -> Any:
        """Envoie une requête et renvoie le JSON, en réessayant sur les erreurs transitoires
        (sauf `retry=False`, pour les écritures non idempotentes)."""
        retries = self._retries if retry else 0
        for attempt in range(retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                error: str = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    if response.is_error:
                        raise ServiceError(
                            f"{self.name} {method} {url} -> HTTP {response.status_code}: "
                            f"{response.text[:300]}",
                            response.status_code,
                        )
                    return response.json()
                error = f"HTTP {response.status_code}"

            if attempt == retries:
                raise ServiceError(f"{self.name} {method} {url} a échoué : {error}")
            delay = 2**attempt
            logger.warning(
                "%s %s %s : %s, nouvelle tentative dans %ss", self.name, method, url, error, delay
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")
