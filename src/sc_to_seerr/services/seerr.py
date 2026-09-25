"""Service Seerr (ex-Overseerr) : recherche, statuts des médias / demandes, création de demandes."""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from sc_to_seerr.models import MediaStatus, MediaType, RequestStatus, SeerrMedia
from sc_to_seerr.services.base import BaseService

logger = logging.getLogger(__name__)

PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class SearchPage:
    results: list[SeerrMedia]
    total_pages: int


@dataclass(frozen=True, slots=True)
class TvDetails:
    tmdb_id: int
    name: str
    original_name: str | None
    year: int | None
    seasons: list[int]  # hors saison 0 (épisodes spéciaux)
    media_status: int | None
    season_statuses: dict[int, int]  # `MediaStatus` par saison connue de Seerr
    requested_seasons: set[int]  # saisons dans une demande non refusée

    def missing_seasons(self) -> list[int]:
        """Saisons ni disponibles, ni en cours, ni déjà demandées."""
        present = {
            n for n, status in self.season_statuses.items()
            if status in (MediaStatus.PENDING, MediaStatus.PROCESSING,
                          MediaStatus.PARTIALLY_AVAILABLE, MediaStatus.AVAILABLE)
        }
        return [n for n in self.seasons if n not in present and n not in self.requested_seasons]


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

    async def search(self, query: str, media_type: MediaType, page: int = 1) -> SearchPage:
        """Recherche TMDB via Seerr (films, séries, personnes) ; ne garde que `media_type`."""
        # Seerr rejette les requêtes dont les espaces sont encodés en "+" : on encode à la main.
        url = f"/search?query={quote(query, safe='')}&page={page}&language={quote(self.language)}"
        data = await self._request("GET", url)
        results = [
            SeerrMedia(
                tmdb_id=r["id"],
                title=r.get("title") or r.get("name") or "",
                original_title=r.get("originalTitle") or r.get("originalName"),
                year=_year(r.get("releaseDate") or r.get("firstAirDate")),
                media_status=(r.get("mediaInfo") or {}).get("status"),
            )
            for r in data.get("results", [])
            if r.get("mediaType") == media_type
        ]
        logger.debug("Seerr : recherche %s %r page %s -> %s résultats", media_type, query, page, len(results))
        return SearchPage(results, data.get("totalPages") or 0)

    async def get_tv(self, tmdb_id: int) -> TvDetails:
        """Fiche d'une série : saisons et ce qui est déjà disponible ou demandé."""
        data = await self._request("GET", f"/tv/{tmdb_id}", params={"language": self.language})
        info = data.get("mediaInfo") or {}
        requested = {
            season["seasonNumber"]
            for request in info.get("requests", [])
            if request.get("status") != RequestStatus.DECLINED
            for season in request.get("seasons", [])
        }
        return TvDetails(
            tmdb_id=tmdb_id,
            name=data.get("name") or "",
            original_name=data.get("originalName"),
            year=_year(data.get("firstAirDate")),
            seasons=sorted(s["seasonNumber"] for s in data.get("seasons", []) if s["seasonNumber"] > 0),
            media_status=info.get("status"),
            season_statuses={s["seasonNumber"]: s["status"] for s in info.get("seasons", [])},
            requested_seasons=requested,
        )

    async def request_movie(self, tmdb_id: int) -> dict[str, Any]:
        """Crée une demande pour un film (au nom de l'utilisateur de la clé API)."""
        payload = {"mediaType": "movie", "mediaId": tmdb_id, "is4k": False}
        return await self._request("POST", "/request", json=payload, retry=False)

    async def request_tv(self, tmdb_id: int, seasons: list[int]) -> dict[str, Any]:
        """Crée une demande pour des saisons d'une série."""
        payload = {"mediaType": "tv", "mediaId": tmdb_id, "seasons": seasons, "is4k": False}
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

    async def get_media_statuses(self, media_type: MediaType) -> dict[int, int]:
        """Statut (`MediaStatus`) de chaque média connu de Seerr, indexé par TMDB id."""
        media = await self._paginate("/media", {"filter": "all", "sort": "added"})
        statuses = {m["tmdbId"]: m["status"] for m in media if m.get("mediaType") == media_type}
        logger.info("Seerr : %s médias %s connus", len(statuses), media_type)
        return statuses

    async def get_request_statuses(self, media_type: MediaType) -> dict[int, list[int]]:
        """Statuts (`RequestStatus`) des demandes, indexés par TMDB id."""
        requests = await self._paginate("/request", {"filter": "all", "sort": "added"})
        statuses: dict[int, list[int]] = defaultdict(list)
        for r in requests:
            media = r.get("media") or {}
            if r.get("type") == media_type and media.get("tmdbId"):
                statuses[media["tmdbId"]].append(r["status"])
        logger.info("Seerr : %s demandes %s", sum(len(v) for v in statuses.values()), media_type)
        return dict(statuses)
