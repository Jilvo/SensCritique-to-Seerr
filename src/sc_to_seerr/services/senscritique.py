"""Service SensCritique : lecture des collections (envies, vus) via l'API GraphQL du site.

SensCritique n'a pas d'API publique officielle ; on utilise l'endpoint GraphQL
(Apollo) du site, sans authentification, sur un profil public.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from sc_to_seerr.models import MediaType, Source, Wish
from sc_to_seerr.services.base import BaseService, ServiceError

logger = logging.getLogger(__name__)

UNIVERSES = {MediaType.MOVIE: "movie", MediaType.TV: "tvShow"}
ACTIONS = {Source.WISH: "WISH", Source.SEEN: "DONE"}  # "DONE" = "Achevés" sur le site

COLLECTION_QUERY = """
query UserCollection(
  $username: String!
  $action: ProductAction
  $universe: String
  $limit: Int
  $offset: Int
  $order: CollectionSort
  $isCollection: Boolean
) {
  user(username: $username) {
    collection(
      action: $action
      universe: $universe
      limit: $limit
      offset: $offset
      order: $order
      isCollection: $isCollection
    ) {
      total
      products {
        id
        title
        originalTitle
        yearOfProduction
        dateRelease
        dateReleaseOriginal
        frenchReleaseDate
        url
      }
    }
  }
}
"""


def _year(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def parse_product(product: dict[str, Any], source: Source = Source.WISH) -> Wish:
    year = product.get("yearOfProduction")
    release_years = {
        y
        for y in (
            year,
            _year(product.get("dateRelease")),
            _year(product.get("dateReleaseOriginal")),
            _year(product.get("frenchReleaseDate")),
        )
        if y
    }
    original = product.get("originalTitle")
    return Wish(
        sc_id=int(product["id"]),
        title=product["title"],
        original_title=original if original and original != product["title"] else None,
        year=year or min(release_years, default=None),
        release_years=frozenset(release_years),
        url=product.get("url"),
        source=source,
    )


class SensCritiqueService(BaseService):
    name = "SensCritique"

    def __init__(self, graphql_url: str, username: str, *, page_size: int = 500, **kwargs: Any):
        super().__init__(graphql_url, headers={"Origin": "https://www.senscritique.com"}, **kwargs)
        self.username = username
        self.page_size = page_size

    async def _fetch_page(
        self, media_type: MediaType, source: Source, offset: int, limit: int | None = None
    ) -> tuple[int, list[dict[str, Any]]]:
        payload = {
            "operationName": "UserCollection",
            "query": COLLECTION_QUERY,
            "variables": {
                "username": self.username,
                "action": ACTIONS[source],
                "universe": UNIVERSES[media_type],
                "limit": limit or self.page_size,
                "offset": offset,
                "order": "LAST_ACTION_DESC",
                "isCollection": True,
            },
        }
        data = await self._request("POST", "", json=payload)
        if data.get("errors"):
            raise ServiceError(f"SensCritique GraphQL : {data['errors'][0].get('message')}")
        user = (data.get("data") or {}).get("user")
        if user is None:
            raise ServiceError(f"Utilisateur SensCritique introuvable : {self.username}")
        collection = user["collection"]
        return collection["total"], collection["products"] or []

    async def count(self, media_type: MediaType, source: Source) -> int:
        total, _ = await self._fetch_page(media_type, source, 0, limit=1)
        return total

    async def iter_collection(self, media_type: MediaType, source: Source) -> AsyncIterator[Wish]:
        """Parcourt une collection (envies ou vus, films ou séries), page par page."""
        offset = 0
        while True:
            total, products = await self._fetch_page(media_type, source, offset)
            logger.debug(
                "SensCritique : %s/%s offset=%s, %s œuvres (total %s)",
                media_type, source, offset, len(products), total,
            )
            for product in products:
                yield parse_product(product, source)
            offset += len(products)
            if not products or offset >= total:
                break

    async def get_collection(self, media_type: MediaType, source: Source, limit: int | None = None) -> list[Wish]:
        items: list[Wish] = []
        async for item in self.iter_collection(media_type, source):
            items.append(item)
            if limit is not None and len(items) >= limit:
                break
        logger.info("SensCritique : %s %s (%s) récupérés pour %s", len(items), media_type, source, self.username)
        return items
