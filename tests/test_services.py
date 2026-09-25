import httpx
import pytest
import respx

from sc_to_seerr.services.base import ServiceError
from sc_to_seerr.services.senscritique import SensCritiqueService, parse_product
from sc_to_seerr.services.seerr import SeerrService

SC_URL = "https://apollo.example/"
SEERR_API = "https://seerr.example/api/v1"


def product(i, title="Film", original=None, year=2020):
    return {
        "id": i,
        "title": title,
        "originalTitle": original,
        "yearOfProduction": year,
        "dateRelease": f"{year}-05-01" if year else None,
        "dateReleaseOriginal": None,
        "frenchReleaseDate": None,
        "url": f"/film/x/{i}",
    }


def test_parse_product_drops_identical_original_title():
    w = parse_product(product(1, "Dune", "Dune", 2021))
    assert w.original_title is None
    assert w.year == 2021
    assert w.sc_url == "https://www.senscritique.com/film/x/1"


def test_parse_product_without_production_year():
    p = product(1, year=None) | {"dateRelease": "2027-01-01"}
    assert parse_product(p).year == 2027


@respx.mock
async def test_senscritique_paginates():
    pages = [
        {"data": {"user": {"collection": {"total": 3, "products": [product(1), product(2)]}}}},
        {"data": {"user": {"collection": {"total": 3, "products": [product(3)]}}}},
    ]
    route = respx.post(SC_URL).mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    async with SensCritiqueService(SC_URL, "moi", page_size=2) as sc:
        wishes = await sc.get_movie_wishes()
    assert [w.sc_id for w in wishes] == [1, 2, 3]
    assert route.call_count == 2


@respx.mock
async def test_senscritique_unknown_user():
    respx.post(SC_URL).mock(return_value=httpx.Response(200, json={"data": {"user": None}}))
    async with SensCritiqueService(SC_URL, "personne") as sc:
        with pytest.raises(ServiceError, match="introuvable"):
            await sc.get_movie_wishes()


@respx.mock
async def test_seerr_search_encodes_query_and_keeps_movies_only():
    route = respx.get(url__startswith=f"{SEERR_API}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "totalPages": 4,
                "results": [
                    {"id": 1, "mediaType": "movie", "title": "Le Parrain", "originalTitle": "The Godfather",
                     "releaseDate": "1972-03-14", "mediaInfo": {"status": 5}},
                    {"id": 2, "mediaType": "tv", "name": "Le Parrain"},
                    {"id": 3, "mediaType": "person", "name": "Quelqu'un"},
                ]
            },
        )
    )
    async with SeerrService(SEERR_API, "key") as seerr:
        result = await seerr.search_movies("Le Parrain !", page=2)
    assert "query=Le%20Parrain%20%21&page=2" in str(route.calls[0].request.url)
    assert route.calls[0].request.headers["X-Api-Key"] == "key"
    assert [(m.tmdb_id, m.year, m.media_status) for m in result.movies] == [(1, 1972, 5)]
    assert result.total_pages == 4


@respx.mock
async def test_seerr_media_and_requests_pagination():
    respx.get(f"{SEERR_API}/media", params={"skip": "0"}).mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": 3}, "results": [
            {"tmdbId": 1, "mediaType": "movie", "status": 5},
            {"tmdbId": 2, "mediaType": "tv", "status": 5},
        ]})
    )
    respx.get(f"{SEERR_API}/media", params={"skip": "2"}).mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": 3}, "results": [
            {"tmdbId": 3, "mediaType": "movie", "status": 2},
        ]})
    )
    respx.get(f"{SEERR_API}/request").mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": 2}, "results": [
            {"type": "movie", "status": 1, "media": {"tmdbId": 3}},
            {"type": "movie", "status": 3, "media": {"tmdbId": 3}},
        ]})
    )
    async with SeerrService(SEERR_API, "key") as seerr:
        assert await seerr.get_movie_media_statuses() == {1: 5, 3: 2}
        assert await seerr.get_movie_request_statuses() == {3: [1, 3]}


@respx.mock
async def test_retry_then_error(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("sc_to_seerr.services.base.asyncio.sleep", no_sleep)
    route = respx.get(f"{SEERR_API}/status").mock(return_value=httpx.Response(503))
    async with SeerrService(SEERR_API, "key", retries=2) as seerr:
        with pytest.raises(ServiceError, match="HTTP 503"):
            await seerr.get_status()
    assert route.call_count == 3


@respx.mock
async def test_client_error_is_not_retried():
    route = respx.get(f"{SEERR_API}/status").mock(return_value=httpx.Response(401, text="Unauthorized"))
    async with SeerrService(SEERR_API, "bad") as seerr:
        with pytest.raises(ServiceError, match="401"):
            await seerr.get_status()
    assert route.call_count == 1
