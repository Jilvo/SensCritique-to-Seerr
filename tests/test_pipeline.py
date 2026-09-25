import pytest

from sc_to_seerr.cache import CachedMatch, MatchCache
from sc_to_seerr.models import MatchMethod, SeerrMovie, Wish, WishStatus
from sc_to_seerr.pipeline import Pipeline, PipelineOptions, classify
from sc_to_seerr.services.seerr import SearchPage


@pytest.mark.parametrize(
    ("media_status", "requests", "expected"),
    [
        (5, [], WishStatus.AVAILABLE),
        (4, [], WishStatus.PARTIALLY_AVAILABLE),
        (3, [2], WishStatus.PROCESSING),
        (2, [1], WishStatus.PENDING),
        (None, [], WishStatus.ABSENT),
        (1, [], WishStatus.ABSENT),
        (1, [3], WishStatus.DECLINED),
        (1, [3, 1], WishStatus.PENDING),
        (7, [], WishStatus.ABSENT),
        (6, [], WishStatus.BLOCKLISTED),
    ],
)
def test_classify(media_status, requests, expected):
    assert classify(media_status, requests) == expected


class FakeSC:
    def __init__(self, wishes):
        self.wishes = wishes

    async def get_movie_wishes(self, limit=None):
        return self.wishes[:limit]


class FakeSeerr:
    def __init__(self, search, media, requests, total_pages=1):
        self.search, self.media, self.requests = search, media, requests
        self.total_pages = total_pages
        self.queries = []

    async def search_movies(self, query, page=1):
        self.queries.append((query, page))
        return SearchPage(self.search.get((query, page), []), self.total_pages)

    async def get_movie_media_statuses(self):
        return self.media

    async def get_movie_request_statuses(self):
        return self.requests


async def test_pipeline_end_to_end(tmp_path):
    wishes = [
        Wish(1, "Évanouis", "Weapons", 2025, frozenset({2025})),
        Wish(2, "Inconnu au bataillon", None, 2001, frozenset({2001})),
        Wish(3, "Déjà en cache", None, 2010, frozenset({2010})),
    ]
    seerr = FakeSeerr(
        search={("Weapons", 1): [SeerrMovie(1078605, "Évanouis", "Weapons", 2025)]},
        media={1078605: 5},
        requests={27205: [1]},
    )
    cache = MatchCache(tmp_path / "matches.json")
    cache.set(3, CachedMatch(27205, "Déjà en cache", 2010, "exact"))

    results = await Pipeline(FakeSC(wishes), seerr, cache, PipelineOptions()).run()

    assert [r.status for r in results] == [WishStatus.AVAILABLE, WishStatus.NOT_FOUND, WishStatus.PENDING]
    assert results[0].match == MatchMethod.EXACT
    assert ("Déjà en cache", 1) not in seerr.queries  # le cache évite la recherche
    reloaded = MatchCache(tmp_path / "matches.json")
    reloaded.load()
    assert reloaded.get(1).tmdb_id == 1078605
    assert reloaded.get(2) is None


def test_manual_cache_entry_is_never_overwritten(tmp_path):
    cache = MatchCache(tmp_path / "m.json")
    cache.set(1, CachedMatch(10, "Manuel", 2000, "manuel"))
    cache.set(1, CachedMatch(99, "Auto", 2000, "exact"))
    cache.clear_automatic()
    assert cache.get(1).tmdb_id == 10


async def test_search_tries_every_query_on_page_one_before_next_pages(tmp_path):
    wishes = [Wish(1, "Hommes", "Men", 2022, frozenset({2022}))]
    seerr = FakeSeerr(search={("Men", 2): [SeerrMovie(10, "Men", None, 2022)]}, media={}, requests={}, total_pages=5)
    results = await Pipeline(FakeSC(wishes), seerr, MatchCache(tmp_path / "m.json"), PipelineOptions()).run()
    assert results[0].tmdb_id == 10
    assert seerr.queries == [("Men", 1), ("Hommes", 1), ("Men", 2)]


async def test_search_stops_at_last_page(tmp_path):
    wishes = [Wish(1, "Rien", None, 2000, frozenset({2000}))]
    seerr = FakeSeerr(search={}, media={}, requests={}, total_pages=1)
    await Pipeline(FakeSC(wishes), seerr, MatchCache(tmp_path / "m.json"), PipelineOptions(search_pages=3)).run()
    assert seerr.queries == [("Rien", 1)]
