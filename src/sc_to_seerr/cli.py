"""Interface en ligne de commande."""

import asyncio
import logging
import random
import sys
from collections import Counter
from collections.abc import Coroutine
from enum import Enum
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from sc_to_seerr.cache import MatchCache
from sc_to_seerr.config import Settings, get_settings
from sc_to_seerr.logging_setup import setup_logging
from sc_to_seerr.models import MatchMethod, MediaStatus, MediaType, Source, WishResult, WishStatus
from sc_to_seerr.pipeline import Pipeline, PipelineOptions
from sc_to_seerr.report import print_details, print_summary, write_csv
from sc_to_seerr.requester import RequestOutcome, RequestResult, request_movie, request_movies, request_series
from sc_to_seerr.services.base import ServiceError
from sc_to_seerr.services.senscritique import SensCritiqueService
from sc_to_seerr.services.seerr import SeerrService, TvDetails

logger = logging.getLogger(__name__)
console = Console()

STATUS_CHOICES = {s.name.lower().replace("_", "-"): s for s in WishStatus}
DEFAULT_SHOWN = ("absent", "not-found", "declined")
SOURCE_CHOICES = {"wish": (Source.WISH,), "seen": (Source.SEEN,), "all": (Source.WISH, Source.SEEN)}


class CacheMode(Enum):
    KEEP = "keep"
    REFRESH = "refresh"  # oublie les correspondances automatiques
    RESET = "reset"  # oublie tout, corrections manuelles comprises


def _build_services(settings: Settings) -> tuple[SensCritiqueService, SeerrService]:
    http = {"timeout": settings.http_timeout, "retries": settings.http_retries}
    sc = SensCritiqueService(
        str(settings.sc_graphql_url), settings.sc_username, page_size=settings.sc_page_size, **http
    )
    return sc, _build_seerr(settings)


def _build_seerr(settings: Settings) -> SeerrService:
    return SeerrService(
        settings.seerr_api_base,
        settings.seerr_api_key.get_secret_value(),
        language=settings.seerr_language,
        timeout=settings.http_timeout,
        retries=settings.http_retries,
    )


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except ServiceError as exc:
        logger.error("%s", exc)
        raise click.ClickException(str(exc)) from exc


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


def _run_pipeline(
    settings: Settings,
    *,
    media_type: MediaType = MediaType.MOVIE,
    sources: tuple[Source, ...] = (Source.WISH,),
    limit: int | None = None,
    concurrency: int | None = None,
    cache_mode: CacheMode = CacheMode.KEEP,
) -> list[WishResult]:
    cache = MatchCache(settings.cache_file if media_type is MediaType.MOVIE else settings.cache_file_tv)
    if cache_mode is CacheMode.RESET:
        cache.reset()
    else:
        cache.load()
        if cache_mode is CacheMode.REFRESH:
            cache.clear_automatic()

    options = PipelineOptions(
        media_type=media_type,
        sources=sources,
        limit=limit,
        concurrency=concurrency or settings.seerr_concurrency,
        year_tolerance=settings.match_year_tolerance,
        search_pages=settings.seerr_search_pages,
    )
    progress = _progress()
    task = progress.add_task("Démarrage", total=None)

    def on_progress(step: str, completed: int, total: int) -> None:
        progress.update(task, description=step, completed=completed, total=total or None)

    async def run() -> list[WishResult]:
        sc, seerr = _build_services(settings)
        async with sc, seerr:
            return await Pipeline(sc, seerr, cache, options, on_progress).run()

    with progress:
        return _run(run())


def _export(
    settings: Settings,
    results: list[WishResult],
    csv_path: Path | None = None,
    media_type: MediaType = MediaType.MOVIE,
    source: str = "wish",
) -> None:
    """CSV par type et par source, pour qu'un export des vus n'écrase pas celui des envies."""
    output = csv_path or (settings.csv_output if media_type is MediaType.MOVIE else settings.csv_output_tv)
    if not csv_path and source != "wish":
        output = output.with_name(f"{output.stem}_{source}{output.suffix}")
    write_csv(output, results)
    console.print(f"CSV : [bold]{output}[/bold] — log : [bold]{settings.log_file}[/bold]")
    logger.info("Terminé : %s envies, CSV %s", len(results), output)


def _requestable(results: list[WishResult], exact_only: bool) -> list[WishResult]:
    return [
        r for r in results
        if r.requestable and (not exact_only or r.match in (MatchMethod.EXACT, MatchMethod.MANUAL))
    ]


def _print_request_results(outcomes: list[RequestResult]) -> None:
    counts = Counter(o.outcome for o in outcomes)
    console.print(
        f"[green]{counts[RequestOutcome.CREATED]} demandé(s)[/green], "
        f"{counts[RequestOutcome.ALREADY]} déjà demandé(s), "
        f"[red]{counts[RequestOutcome.FAILED]} échec(s)[/red]"
    )
    for o in outcomes:
        if o.outcome is RequestOutcome.FAILED:
            console.print(f"  [red]✘[/red] {escape(o.result.seerr_title or '')} ({o.result.seerr_year}) : {escape(o.message)}")


# --- options communes -------------------------------------------------------

exact_only_option = click.option(
    "--exact-only", is_flag=True, help="Ignore les correspondances approchées (titre pas strictement identique)."
)
dry_run_option = click.option("--dry-run", is_flag=True, help="Affiche ce qui serait demandé, sans rien envoyer.")
yes_option = click.option("-y", "--yes", is_flag=True, help="Ne demande pas de confirmation.")


def source_option(default: str = "wish"):  # noqa: ANN201
    return click.option(
        "--source",
        type=click.Choice(list(SOURCE_CHOICES)),
        default=default,
        show_default=True,
        help="Collection SensCritique : envies (wish), vus (seen) ou les deux (all).",
    )


show_option = click.option(
    "--show",
    "shown",
    multiple=True,
    type=click.Choice([*STATUS_CHOICES, "all"]),
    default=DEFAULT_SHOWN,
    show_default=True,
    help="Statuts à détailler en console (répétable).",
)
limit_option = click.option(
    "--limit", type=click.IntRange(min=1), help="Ne traite que les N œuvres les plus récentes (par source)."
)
csv_option = click.option(
    "--csv", "csv_path", type=click.Path(dir_okay=False, path_type=Path), help="Fichier CSV de sortie."
)
concurrency_option = click.option("--concurrency", type=click.IntRange(1, 20), help="Recherches Seerr simultanées.")
refresh_option = click.option(
    "--refresh-cache", is_flag=True, help="Refait les correspondances automatiques (garde les manuelles)."
)


def _shown_statuses(shown: tuple[str, ...]) -> set[WishStatus]:
    return set(WishStatus) if "all" in shown else {STATUS_CHOICES[s] for s in shown}


# --- commandes ---------------------------------------------------------------


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Affiche aussi les logs détaillés en console.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Vérifie si les envies de films SensCritique sont disponibles ou demandées dans Seerr."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        missing = ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        raise click.ClickException(f"Configuration invalide ou incomplète dans .env : {missing}") from exc
    setup_logging(settings.log_file, settings.log_level, console, verbose)
    logger.info("Commande : %s", " ".join(sys.argv[1:]))
    ctx.obj = settings


@main.command()
@click.pass_obj
def ping(settings: Settings) -> None:
    """Teste la connexion à SensCritique et à Seerr."""

    async def run() -> None:
        sc, seerr = _build_services(settings)
        async with sc, seerr:
            status = await seerr.get_status()
            console.print(f"[green]✔[/green] Seerr {status.get('version')} joignable ({settings.seerr_url})")
            counts = await asyncio.gather(
                sc.count(MediaType.MOVIE, Source.WISH),
                sc.count(MediaType.MOVIE, Source.SEEN),
                sc.count(MediaType.TV, Source.WISH),
            )
            console.print(
                f"[green]✔[/green] SensCritique ({settings.sc_username}) : {counts[0]} envies de films, "
                f"{counts[1]} films vus, {counts[2]} envies de séries"
            )

    _run(run())


@main.command()
@source_option()
@limit_option
@show_option
@refresh_option
@concurrency_option
@csv_option
@click.pass_obj
def check(
    settings: Settings,
    source: str,
    limit: int | None,
    shown: tuple[str, ...],
    refresh_cache: bool,
    concurrency: int | None,
    csv_path: Path | None,
) -> None:
    """Vérifie le statut de chaque film (envies et/ou vus) dans Seerr."""
    mode = CacheMode.REFRESH if refresh_cache else CacheMode.KEEP
    results = _run_pipeline(
        settings, sources=SOURCE_CHOICES[source], limit=limit, concurrency=concurrency, cache_mode=mode
    )
    print_details(console, results, _shown_statuses(shown))
    print_summary(console, results)
    _export(settings, results, csv_path, source=source)


@main.command("rebuild-cache")
@click.option("--series", is_flag=True, help="Refait le cache des séries au lieu de celui des films.")
@source_option(default="all")
@click.option("--keep-manual", is_flag=True, help="Garde les corrections manuelles.")
@concurrency_option
@yes_option
@click.pass_obj
def rebuild_cache(
    settings: Settings, series: bool, source: str, keep_manual: bool, concurrency: int | None, yes: bool
) -> None:
    """Refait un cache des correspondances de zéro (une recherche Seerr par œuvre)."""
    media_type = MediaType.TV if series else MediaType.MOVIE
    cache_file = settings.cache_file_tv if series else settings.cache_file
    if not keep_manual and not yes:
        click.confirm(f"Supprimer tout le cache {cache_file}, corrections manuelles comprises ?", abort=True)
    mode = CacheMode.REFRESH if keep_manual else CacheMode.RESET
    results = _run_pipeline(
        settings, media_type=media_type, sources=SOURCE_CHOICES[source], concurrency=concurrency, cache_mode=mode
    )
    print_summary(console, results, "séries" if series else "films")
    _export(settings, results, media_type=media_type, source=source)


@main.command("random")
@source_option()
@exact_only_option
@dry_run_option
@yes_option
@click.pass_obj
def random_request(settings: Settings, source: str, exact_only: bool, dry_run: bool, yes: bool) -> None:
    """Tire au sort un film absent de Seerr et le demande."""
    candidates = _requestable(_run_pipeline(settings, sources=SOURCE_CHOICES[source]), exact_only)
    random.shuffle(candidates)
    if not candidates:
        console.print("Aucune envie à demander : tout est déjà disponible ou demandé.")
        return

    for pick in candidates:
        console.print(
            Panel.fit(
                f"[bold]{escape(pick.wish.title)}[/bold] ({pick.wish.year or '?'})"
                + (f"\n[dim]{escape(pick.wish.original_title)}[/dim]" if pick.wish.original_title else "")
                + f"\n\nSeerr : {escape(pick.seerr_title or '')} ({pick.seerr_year or '?'})"
                + (" [yellow]~ correspondance approchée[/yellow]" if pick.match == MatchMethod.FUZZY else "")
                + f"\nSensCritique : {pick.wish.sc_url}"
                + f"\nTMDB : https://www.themoviedb.org/movie/{pick.tmdb_id}",
                title=f"🎲 Tirage parmi {len(candidates)} films ({pick.wish.source.value})",
            )
        )
        if dry_run:
            return
        if not yes:
            choice = click.prompt(
                "Demander ce film ? (o)ui / (a)utre / (q)uitter",
                type=click.Choice(["o", "a", "q"]),
                default="o",
                show_choices=False,
            )
            if choice == "q":
                return
            if choice == "a":
                continue

        async def send(result: WishResult = pick) -> RequestResult:
            async with _build_seerr(settings) as seerr:
                return await request_movie(seerr, result)

        _print_request_results([_run(send())])
        return
    console.print("Plus d'autre envie à proposer.")


@main.command("request-all")
@source_option()
@click.option("--limit", type=click.IntRange(min=1), help="Nombre maximum de demandes.")
@click.option("--random", "shuffle", is_flag=True, help="Ordre aléatoire (défaut : envies les plus récentes d'abord).")
@exact_only_option
@dry_run_option
@yes_option
@click.pass_obj
def request_all(
    settings: Settings, source: str, limit: int | None, shuffle: bool, exact_only: bool, dry_run: bool, yes: bool
) -> None:
    """Demande en masse les films absents de Seerr."""
    candidates = _requestable(_run_pipeline(settings, sources=SOURCE_CHOICES[source]), exact_only)
    if shuffle:
        random.shuffle(candidates)
    candidates = candidates[:limit]
    if not candidates:
        console.print("Aucune envie à demander : tout est déjà disponible ou demandé.")
        return

    print_details(console, candidates, {WishStatus.ABSENT}, title="À demander", sort=False)
    if dry_run:
        console.print(f"[yellow]--dry-run[/yellow] : {len(candidates)} film(s) seraient demandés.")
        return
    if not yes:
        click.confirm(f"Demander ces {len(candidates)} film(s) dans Seerr ?", abort=True)

    progress = _progress()
    task = progress.add_task("Demandes", total=len(candidates))

    async def send() -> list[RequestResult]:
        async with _build_seerr(settings) as seerr:
            return await request_movies(
                seerr, candidates, lambda done, total: progress.update(task, completed=done)
            )

    with progress:
        outcomes = _run(send())
    _print_request_results(outcomes)


# --- séries ------------------------------------------------------------------

SEASON_LABELS = {
    MediaStatus.AVAILABLE: "[green]disponible[/green]",
    MediaStatus.PARTIALLY_AVAILABLE: "[yellow]partiellement disponible[/yellow]",
    MediaStatus.PROCESSING: "[cyan]en cours[/cyan]",
    MediaStatus.PENDING: "[blue]demandée[/blue]",
}


def _parse_seasons(value: str, available: list[int]) -> list[int]:
    if value.strip().lower() == "all":
        return available
    try:
        seasons = sorted({int(part) for part in value.split(",") if part.strip()})
    except ValueError as exc:
        raise click.BadParameter("format attendu : all ou 1,2,3", param_hint="--seasons") from exc
    unknown = [n for n in seasons if n not in available]
    if unknown:
        raise click.BadParameter(
            f"saison(s) inexistante(s) : {unknown} (existantes : {available})", param_hint="--seasons"
        )
    return seasons


@main.group()
def series() -> None:
    """Séries : état des envies SensCritique et demandes par ID TMDB."""


@series.command("list")
@source_option()
@limit_option
@show_option
@refresh_option
@concurrency_option
@csv_option
@click.pass_obj
def series_list(
    settings: Settings,
    source: str,
    limit: int | None,
    shown: tuple[str, ...],
    refresh_cache: bool,
    concurrency: int | None,
    csv_path: Path | None,
) -> None:
    """Statut de chaque série SensCritique dans Seerr, avec son ID TMDB pour `series request`."""
    mode = CacheMode.REFRESH if refresh_cache else CacheMode.KEEP
    results = _run_pipeline(
        settings,
        media_type=MediaType.TV,
        sources=SOURCE_CHOICES[source],
        limit=limit,
        concurrency=concurrency,
        cache_mode=mode,
    )
    print_details(console, results, _shown_statuses(shown))
    print_summary(console, results, "séries")
    _export(settings, results, csv_path, MediaType.TV, source)
    console.print("Pour demander une série : [bold]sc-to-seerr series request <ID TMDB>[/bold]")


@series.command("request")
@click.argument("tmdb_id", type=click.IntRange(min=1))
@click.option(
    "--seasons",
    default="all",
    show_default=True,
    help="Saisons à demander : all (toutes celles qui manquent) ou liste, ex. 1,2,3.",
)
@dry_run_option
@yes_option
@click.pass_obj
def series_request(settings: Settings, tmdb_id: int, seasons: str, dry_run: bool, yes: bool) -> None:
    """Demande une série dans Seerr à partir de son ID TMDB (voir `series list`)."""

    async def fetch() -> TvDetails:
        async with _build_seerr(settings) as seerr:
            return await seerr.get_tv(tmdb_id)

    try:
        show = asyncio.run(fetch())
    except ServiceError as exc:
        if exc.status_code == 404:
            raise click.ClickException(f"Aucune série TMDB avec l'ID {tmdb_id}.") from exc
        logger.error("%s", exc)
        raise click.ClickException(str(exc)) from exc

    lines = [f"[bold]{escape(show.name)}[/bold] ({show.year or '?'})"]
    if show.original_name and show.original_name != show.name:
        lines.append(f"[dim]{escape(show.original_name)}[/dim]")
    lines.append("")
    for n in show.seasons:
        status = show.season_statuses.get(n)
        if status in SEASON_LABELS:
            label = SEASON_LABELS[MediaStatus(status)]
        elif n in show.requested_seasons:
            label = SEASON_LABELS[MediaStatus.PENDING]
        else:
            label = "[magenta]absente[/magenta]"
        lines.append(f"Saison {n} : {label}")
    lines.append(f"\nTMDB : https://www.themoviedb.org/tv/{tmdb_id}")
    console.print(Panel.fit("\n".join(lines), title="📺 Série"))

    wanted = _parse_seasons(seasons, show.seasons)
    missing = set(show.missing_seasons())
    skipped = [n for n in wanted if n not in missing]
    to_request = [n for n in wanted if n in missing]
    if skipped:
        console.print(f"Déjà disponibles ou demandées, ignorées : saison(s) {', '.join(map(str, skipped))}")
    if not to_request:
        console.print("Rien à demander.")
        return
    summary = f"{show.name} — saison(s) {', '.join(map(str, to_request))}"
    if dry_run:
        console.print(f"[yellow]--dry-run[/yellow] : serait demandé : {escape(summary)}")
        return
    if not yes:
        click.confirm(f"Demander {summary} ?", abort=True)

    async def send() -> tuple[RequestOutcome, str]:
        async with _build_seerr(settings) as seerr:
            return await request_series(seerr, tmdb_id, to_request)

    outcome, message = _run(send())
    if outcome is RequestOutcome.FAILED:
        raise click.ClickException(f"Échec de la demande : {message}")
    console.print(f"[green]✔[/green] {escape(summary)} : {outcome.value}")
