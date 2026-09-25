import json

import httpx
import pytest
import respx
from click.testing import CliRunner

from sc_to_seerr import cli
from sc_to_seerr.cache import CachedMatch, MatchCache
from sc_to_seerr.models import MatchMethod, Wish, WishResult, WishStatus
from sc_to_seerr.requester import RequestOutcome, RequestResult, request_movies
from sc_to_seerr.services.seerr import SeerrService

SEERR_API = "https://seerr.example/api/v1"


def result(tmdb_id, status=WishStatus.ABSENT, match=MatchMethod.EXACT):
    wish = Wish(tmdb_id, f"Film {tmdb_id}", None, 2020, frozenset({2020}))
    return WishResult(wish, status, match, tmdb_id, f"Film {tmdb_id}", 2020)


@respx.mock
async def test_request_movie_payload_and_no_retry():
    route = respx.post(f"{SEERR_API}/request").mock(return_value=httpx.Response(503))
    async with SeerrService(SEERR_API, "key", retries=3) as seerr:
        outcomes = await request_movies(seerr, [result(42)])
    assert route.call_count == 1  # une écriture n'est jamais rejouée
    assert json.loads(route.calls[0].request.content) == {"mediaType": "movie", "mediaId": 42, "is4k": False}
    assert outcomes[0].outcome is RequestOutcome.FAILED


@respx.mock
async def test_request_movies_continues_after_errors():
    respx.post(f"{SEERR_API}/request").mock(
        side_effect=[
            httpx.Response(201, json={"id": 1, "status": 2}),
            httpx.Response(409, json={"message": "Request for this media already exists."}),
            httpx.Response(500),
            httpx.Response(201, json={"id": 2, "status": 2}),
        ]
    )
    progress = []
    async with SeerrService(SEERR_API, "key") as seerr:
        outcomes = await request_movies(seerr, [result(i) for i in range(4)], lambda d, t: progress.append(d))
    assert [o.outcome for o in outcomes] == [
        RequestOutcome.CREATED, RequestOutcome.ALREADY, RequestOutcome.FAILED, RequestOutcome.CREATED
    ]
    assert progress == [1, 2, 3, 4]


def test_requestable():
    assert result(1).requestable
    assert not result(1, WishStatus.PENDING).requestable
    assert not result(1, WishStatus.BLOCKLISTED).requestable
    assert not WishResult(result(1).wish, WishStatus.NOT_FOUND, MatchMethod.NONE).requestable


def test_cache_reset_drops_manual_entries(tmp_path):
    path = tmp_path / "m.json"
    cache = MatchCache(path)
    cache.set(1, CachedMatch(10, "Manuel", 2000, "manuel"))
    cache.save()
    cache = MatchCache(path)
    cache.reset()
    cache.save()
    reloaded = MatchCache(path)
    reloaded.load()
    assert len(reloaded) == 0


# --- CLI ---------------------------------------------------------------------

RESULTS = [
    result(1),
    result(2, WishStatus.AVAILABLE),
    result(3, match=MatchMethod.FUZZY),
    result(4),
]


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """CLI avec un pipeline et des demandes factices : rien ne sort vers le réseau."""
    monkeypatch.setenv("SC_USERNAME", "moi")
    monkeypatch.setenv("SEERR_URL", "https://seerr.example")
    monkeypatch.setenv("SEERR_API_KEY", "key")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "test.log"))
    cli.get_settings.cache_clear()
    sent: list[int] = []

    monkeypatch.setattr(cli, "_run_pipeline", lambda settings, **kw: list(RESULTS))

    async def fake_request_movie(seerr, r):
        sent.append(r.tmdb_id)
        return RequestResult(r, RequestOutcome.CREATED)

    async def fake_request_movies(seerr, results, on_progress=None):
        return [await fake_request_movie(seerr, r) for r in results]

    monkeypatch.setattr(cli, "request_movie", fake_request_movie)
    monkeypatch.setattr(cli, "request_movies", fake_request_movies)
    yield sent
    cli.get_settings.cache_clear()


def invoke(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input, catch_exceptions=False)


def test_request_all_dry_run_sends_nothing(fake_cli):
    out = invoke("request-all", "--dry-run")
    assert "3 film(s) seraient demandés" in out.output
    assert fake_cli == []


def test_request_all_aborts_without_confirmation(fake_cli):
    out = invoke("request-all", input="n\n")
    assert out.exit_code == 1
    assert fake_cli == []


def test_request_all_with_filters(fake_cli):
    invoke("request-all", "--yes", "--exact-only", "--limit", "1")
    assert fake_cli == [1]
    fake_cli.clear()
    invoke("request-all", "--yes")
    assert fake_cli == [1, 3, 4]  # ordre des envies, films déjà disponibles exclus


def test_random_request(fake_cli):
    invoke("random", "--yes")
    assert len(fake_cli) == 1 and fake_cli[0] in (1, 3, 4)


def test_random_quit_and_reroll(fake_cli):
    invoke("random", input="q\n")
    assert fake_cli == []
    invoke("random", input="a\no\n")
    assert len(fake_cli) == 1


def test_random_dry_run(fake_cli):
    out = invoke("random", "--dry-run")
    assert "Tirage parmi 3 envies" in out.output
    assert fake_cli == []


def test_rebuild_cache_asks_confirmation(fake_cli, monkeypatch):
    modes = []
    monkeypatch.setattr(cli, "_run_pipeline", lambda settings, **kw: modes.append(kw["cache_mode"]) or [])
    monkeypatch.setattr(cli, "_export", lambda *a, **kw: None)
    assert invoke("rebuild-cache", input="n\n").exit_code == 1
    assert modes == []
    invoke("rebuild-cache", "--yes")
    invoke("rebuild-cache", "--keep-manual")
    assert modes == [cli.CacheMode.RESET, cli.CacheMode.REFRESH]
