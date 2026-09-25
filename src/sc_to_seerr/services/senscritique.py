"""Service SensCritique : lecture des envies via l'API GraphQL du site.

SensCritique n'a pas d'API publique officielle ; on utilise l'endpoint GraphQL
(Apollo) du site, sans authentification, sur un profil public.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from sc_to_seerr.models import Wish
from sc_to_seerr.services.base import BaseService, ServiceError

logger = logging.getLogger(__name__)

WISHLIST_QUERY = """
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


def parse_product(product: dict[str, Any]) -> Wish:
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
    )


class SensCritiqueService(BaseService):
    name = "SensCritique"

    def __init__(self, graphql_url: str, username: str, *, page_size: int = 500, **kwargs: Any):
        super().__init__(graphql_url, headers={"Origin": "https://www.senscritique.com"}, **kwargs)
        self.username = username
        self.page_size = page_size

    async def _fetch_page(self, offset: int, limit: int | None = None) -> tuple[int, list[dict[str, Any]]]:
        payload = {
            "operationName": "UserCollection",
            "query": WISHLIST_QUERY,
            "variables": {
                "username": self.username,
                "action": "WISH",
                "universe": "movie",
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

    async def count_movie_wishes(self) -> int:
        total, _ = await self._fetch_page(0, limit=1)
        return total

    async def iter_movie_wishes(self) -> AsyncIterator[Wish]:
        """Parcourt toutes les envies de films, page par page."""
        offset = 0
        while True:
            total, products = await self._fetch_page(offset)
            logger.debug("SensCritique : page offset=%s, %s films (total %s)", offset, len(products), total)
            for product in products:
                yield parse_product(product)
            offset += len(products)
            if not products or offset >= total:
                break

    async def get_movie_wishes(self, limit: int | None = None) -> list[Wish]:
        wishes: list[Wish] = []
        async for wish in self.iter_movie_wishes():
            wishes.append(wish)
            if limit is not None and len(wishes) >= limit:
                break
        logger.info("SensCritique : %s envies de films récupérées pour %s", len(wishes), self.username)
        return wishes
