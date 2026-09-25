"""Création de demandes Seerr pour les envies absentes."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sc_to_seerr.models import WishResult
from sc_to_seerr.services.base import ServiceError
from sc_to_seerr.services.seerr import SeerrService

logger = logging.getLogger(__name__)


class RequestOutcome(StrEnum):
    CREATED = "demandé"
    ALREADY = "déjà demandé"
    FAILED = "échec"


@dataclass(slots=True)
class RequestResult:
    result: WishResult
    outcome: RequestOutcome
    message: str = ""


async def request_movie(seerr: SeerrService, result: WishResult) -> RequestResult:
    assert result.tmdb_id is not None
    label = f"{result.seerr_title} ({result.seerr_year}) [tmdb {result.tmdb_id}]"
    try:
        response = await seerr.request_movie(result.tmdb_id)
    except ServiceError as exc:
        if exc.status_code == 409:
            logger.info("Demande déjà existante : %s", label)
            return RequestResult(result, RequestOutcome.ALREADY)
        logger.error("Demande impossible : %s : %s", label, exc)
        return RequestResult(result, RequestOutcome.FAILED, str(exc))
    logger.info("Demande créée : %s (demande n°%s, statut %s)", label, response.get("id"), response.get("status"))
    return RequestResult(result, RequestOutcome.CREATED)


async def request_series(seerr: SeerrService, tmdb_id: int, seasons: list[int]) -> tuple[RequestOutcome, str]:
    label = f"série tmdb {tmdb_id}, saisons {seasons}"
    try:
        response = await seerr.request_tv(tmdb_id, seasons)
    except ServiceError as exc:
        if exc.status_code == 409:
            logger.info("Demande déjà existante : %s", label)
            return RequestOutcome.ALREADY, ""
        logger.error("Demande impossible : %s : %s", label, exc)
        return RequestOutcome.FAILED, str(exc)
    logger.info("Demande créée : %s (demande n°%s, statut %s)", label, response.get("id"), response.get("status"))
    return RequestOutcome.CREATED, ""


async def request_movies(
    seerr: SeerrService,
    results: list[WishResult],
    on_progress: Callable[[int, int], None] | None = None,
) -> list[RequestResult]:
    """Demandes une par une, pour ménager Seerr et Radarr ; une erreur n'arrête pas le lot."""
    outcomes: list[RequestResult] = []
    for i, result in enumerate(results, 1):
        outcomes.append(await request_movie(seerr, result))
        if on_progress:
            on_progress(i, len(results))
    return outcomes
