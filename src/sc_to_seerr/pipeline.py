"""Orchestration : collection SensCritique -> correspondance TMDB -> statut Seerr."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sc_to_seerr.cache import CachedMatch, MatchCache
from sc_to_seerr.matching import find_match, search_queries
from sc_to_seerr.models import (
    MatchMethod,
    MediaStatus,
    MediaType,
    RequestStatus,
    Source,
    Wish,
    WishResult,
    WishStatus,
)
from sc_to_seerr.services.base import ServiceError
from sc_to_seerr.services.senscritique import SensCritiqueService
from sc_to_seerr.services.seerr import SeerrService

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]
"""(étape, avancement, total)"""

MEDIA_STATUS_MAP = {
    MediaStatus.AVAILABLE: WishStatus.AVAILABLE,
    MediaStatus.PARTIALLY_AVAILABLE: WishStatus.PARTIALLY_AVAILABLE,
    MediaStatus.PROCESSING: WishStatus.PROCESSING,
    MediaStatus.PENDING: WishStatus.PENDING,
}


def classify(media_status: int | None, request_statuses: list[int]) -> WishStatus:
    """Statut d'une envie à partir du statut du média et de ses demandes dans Seerr."""
    if media_status in MEDIA_STATUS_MAP:
        return MEDIA_STATUS_MAP[MediaStatus(media_status)]
    if media_status == MediaStatus.BLOCKLISTED:
        return WishStatus.BLOCKLISTED
    if RequestStatus.PENDING in request_statuses:
        return WishStatus.PENDING
    if RequestStatus.APPROVED in request_statuses:
        return WishStatus.PROCESSING
    if request_statuses and all(s == RequestStatus.DECLINED for s in request_statuses):
        return WishStatus.DECLINED
    return WishStatus.ABSENT


@dataclass(slots=True)
class PipelineOptions:
    media_type: MediaType = MediaType.MOVIE
    sources: tuple[Source, ...] = (Source.WISH,)
    limit: int | None = None  # par source
    concurrency: int = 5
    year_tolerance: int = 1
    search_pages: int = 3


class Pipeline:
    def __init__(
        self,
        senscritique: SensCritiqueService,
        seerr: SeerrService,
        cache: MatchCache,
        options: PipelineOptions,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self.sc = senscritique
        self.seerr = seerr
        self.cache = cache
        self.options = options
        self._progress = on_progress or (lambda *_: None)

    async def _search(self, wish: Wish) -> CachedMatch | None:
        """Essaie chaque requête page 1, puis les pages suivantes (titres courts noyés
        dans les résultats populaires : "Men", "Z"...)."""
        queries = search_queries(wish)
        for page in range(1, self.options.search_pages + 1):
            for query in list(queries):
                result = await self.seerr.search(query, self.options.media_type, page)
                if page >= result.total_pages:
                    queries.remove(query)
                media, method = find_match(wish, result.results, self.options.year_tolerance)
                if media:
                    logger.debug(
                        "Correspondance %s : %r -> %s (%r, page %s)", method, wish.title, media.tmdb_id, query, page
                    )
                    return CachedMatch(media.tmdb_id, media.title, media.year, method.value)
            if not queries:
                break
        return None

    async def _resolve(self, wish: Wish, semaphore: asyncio.Semaphore) -> CachedMatch | None:
        cached = self.cache.get(wish.sc_id)
        if cached:
            return cached
        async with semaphore:
            try:
                match = await self._search(wish)
            except ServiceError as exc:
                logger.error("Recherche impossible pour %r (%s) : %s", wish.title, wish.year, exc)
                return None
        if match:
            self.cache.set(wish.sc_id, match)
        else:
            logger.warning(
                "Aucune correspondance pour %r / %r (%s) %s",
                wish.title, wish.original_title, wish.year, wish.sc_url,
            )
        return match

    async def _fetch_wishes(self) -> list[Wish]:
        """Œuvres des collections demandées, sans doublon (une œuvre peut être vue et en envie)."""
        collections = await asyncio.gather(
            *(self.sc.get_collection(self.options.media_type, s, self.options.limit) for s in self.options.sources)
        )
        unique: dict[int, Wish] = {}
        for items in collections:
            for item in items:
                unique.setdefault(item.sc_id, item)
        return list(unique.values())

    async def run(self) -> list[WishResult]:
        self._progress("SensCritique et statuts Seerr", 0, 0)
        media_type = self.options.media_type
        wishes, media_statuses, request_statuses = await asyncio.gather(
            self._fetch_wishes(),
            self.seerr.get_media_statuses(media_type),
            self.seerr.get_request_statuses(media_type),
        )

        semaphore = asyncio.Semaphore(self.options.concurrency)
        done = 0
        total = len(wishes)
        self._progress("Rapprochement", done, total)

        async def resolve(wish: Wish) -> CachedMatch | None:
            nonlocal done
            match = await self._resolve(wish, semaphore)
            done += 1
            self._progress("Rapprochement", done, total)
            return match

        try:
            matches = await asyncio.gather(*(resolve(w) for w in wishes))
        finally:
            self.cache.save()

        results: list[WishResult] = []
        for wish, match in zip(wishes, matches, strict=True):
            if match is None:
                results.append(WishResult(wish, WishStatus.NOT_FOUND, MatchMethod.NONE))
                continue
            status = classify(media_statuses.get(match.tmdb_id), request_statuses.get(match.tmdb_id, []))
            results.append(
                WishResult(wish, status, MatchMethod(match.method), match.tmdb_id, match.title, match.year)
            )
            logger.info("%s (%s) -> %s [tmdb %s]", wish.title, wish.year, status.value, match.tmdb_id)
        return results
