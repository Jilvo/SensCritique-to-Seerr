"""Service Seerr (ex-Overseerr) : recherche de films et statuts des médias / demandes."""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sc_to_seerr.models import SeerrMovie
from sc_to_seerr.services.base import BaseService

logger = logging.getLogger(__name__)

PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class SearchPage:
    movies: list[SeerrMovie]
    total_pages: int


def _year(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


class SeerrService(BaseService):
    name = "Seerr"

    def __init__(self, api_base: str, api_key: str, *, language: str = "fr", **kwargs: Any):
        super().__init__(api_base, headers={"X-Api-Key": api_key, "Accept": "application/json"}, **kwargs)
        self.language = language

    async def get_status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def search_movies(self, query: str, page: int = 1) -> SearchPage:
        """Recherche TMDB via Seerr (films, séries, personnes) ; ne garde que les films."""
        # Seerr rejette les requêtes dont les espaces sont encodés en "+" : on encode à la main.
        url = f"/search?query={quote(query, safe='')}&page={page}&language={quote(self.language)}"
        data = await self._request("GET", url)
        movies = [
            SeerrMovie(
                tmdb_id=r["id"],
                title=r.get("title") or "",
                original_title=r.get("originalTitle"),
                year=_year(r.get("releaseDate")),
                media_status=(r.get("mediaInfo") or {}).get("status"),
            )
            for r in data.get("results", [])
            if r.get("mediaType") == "movie"
        ]
        logger.debug("Seerr : recherche %r page %s -> %s films", query, page, len(movies))
        return SearchPage(movies, data.get("totalPages") or 0)

    async def request_movie(self, tmdb_id: int) -> dict[str, Any]:
        """Crée une demande pour un film (au nom de l'utilisateur de la clé API)."""
        payload = {"mediaType": "movie", "mediaId": tmdb_id, "is4k": False}
        return await self._request("POST", "/request", json=payload, retry=False)

    async def _paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        skip = 0
        while True:
            data = await self._request("GET", path, params={**params, "take": PAGE_SIZE, "skip": skip})
            page = data.get("results", [])
            results.extend(page)
            skip += len(page)
            if not page or skip >= data["pageInfo"]["results"]:
                return results

    async def get_movie_media_statuses(self) -> dict[int, int]:
        """Statut (`MediaStatus`) de chaque film connu de Seerr, indexé par TMDB id."""
        media = await self._paginate("/media", {"filter": "all", "sort": "added"})
        statuses = {m["tmdbId"]: m["status"] for m in media if m.get("mediaType") == "movie"}
        logger.info("Seerr : %s films connus", len(statuses))
        return statuses

    async def get_movie_request_statuses(self) -> dict[int, list[int]]:
        """Statuts (`RequestStatus`) des demandes de films, indexés par TMDB id."""
        requests = await self._paginate("/request", {"filter": "all", "sort": "added"})
        statuses: dict[int, list[int]] = defaultdict(list)
        for r in requests:
            media = r.get("media") or {}
            if r.get("type") == "movie" and media.get("tmdbId"):
                statuses[media["tmdbId"]].append(r["status"])
        logger.info("Seerr : %s demandes de films", sum(len(v) for v in statuses.values()))
        return dict(statuses)
