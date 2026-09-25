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
from sc_to_seerr.models import MatchMethod, WishResult, WishStatus
from sc_to_seerr.pipeline import Pipeline, PipelineOptions
from sc_to_seerr.report import print_details, print_summary, write_csv
from sc_to_seerr.requester import RequestOutcome, RequestResult, request_movie, request_movies
from sc_to_seerr.services.base import ServiceError
from sc_to_seerr.services.senscritique import SensCritiqueService
from sc_to_seerr.services.seerr import SeerrService

logger = logging.getLogger(__name__)
console = Console()

STATUS_CHOICES = {s.name.lower().replace("_", "-"): s for s in WishStatus}
DEFAULT_SHOWN = ("absent", "not-found", "declined")


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
    limit: int | None = None,
    concurrency: int | None = None,
    cache_mode: CacheMode = CacheMode.KEEP,
) -> list[WishResult]:
    cache = MatchCache(settings.cache_file)
    if cache_mode is CacheMode.RESET:
        cache.reset()
    else:
        cache.load()
        if cache_mode is CacheMode.REFRESH:
            cache.clear_automatic()

    options = PipelineOptions(
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


def _export(settings: Settings, results: list[WishResult], csv_path: Path | None = None) -> None:
    output = csv_path or settings.csv_output
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
            total = await sc.count_movie_wishes()
            console.print(f"[green]✔[/green] SensCritique : {total} envies de films pour {settings.sc_username}")

    _run(run())


@main.command()
@click.option("--limit", type=click.IntRange(min=1), help="Ne traite que les N envies les plus récentes.")
@click.option(
    "--show",
    "shown",
    multiple=True,
    type=click.Choice([*STATUS_CHOICES, "all"]),
    default=DEFAULT_SHOWN,
    show_default=True,
    help="Statuts à détailler en console (répétable).",
)
@click.option("--refresh-cache", is_flag=True, help="Refait les correspondances automatiques (garde les manuelles).")
@click.option("--concurrency", type=click.IntRange(1, 20), help="Recherches Seerr simultanées.")
@click.option("--csv", "csv_path", type=click.Path(dir_okay=False, path_type=Path), help="Fichier CSV de sortie.")
@click.pass_obj
def check(
    settings: Settings,
    limit: int | None,
    shown: tuple[str, ...],
    refresh_cache: bool,
    concurrency: int | None,
    csv_path: Path | None,
) -> None:
    """Vérifie le statut de chaque envie de film dans Seerr."""
    mode = CacheMode.REFRESH if refresh_cache else CacheMode.KEEP
    results = _run_pipeline(settings, limit=limit, concurrency=concurrency, cache_mode=mode)

    statuses = set(WishStatus) if "all" in shown else {STATUS_CHOICES[s] for s in shown}
    print_details(console, results, statuses)
    print_summary(console, results)
    _export(settings, results, csv_path)


@main.command("rebuild-cache")
@click.option("--keep-manual", is_flag=True, help="Garde les corrections manuelles.")
@click.option("--concurrency", type=click.IntRange(1, 20), help="Recherches Seerr simultanées.")
@yes_option
@click.pass_obj
def rebuild_cache(settings: Settings, keep_manual: bool, concurrency: int | None, yes: bool) -> None:
    """Refait le cache des correspondances de zéro (une recherche Seerr par envie)."""
    if not keep_manual and not yes:
        click.confirm(
            f"Supprimer tout le cache {settings.cache_file}, corrections manuelles comprises ?", abort=True
        )
    mode = CacheMode.REFRESH if keep_manual else CacheMode.RESET
    results = _run_pipeline(settings, concurrency=concurrency, cache_mode=mode)
    print_summary(console, results)
    _export(settings, results)


@main.command("random")
@exact_only_option
@dry_run_option
@yes_option
@click.pass_obj
def random_request(settings: Settings, exact_only: bool, dry_run: bool, yes: bool) -> None:
    """Tire au sort une envie absente de Seerr et la demande."""
    candidates = _requestable(_run_pipeline(settings), exact_only)
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
                title=f"🎲 Tirage parmi {len(candidates)} envies",
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
@click.option("--limit", type=click.IntRange(min=1), help="Nombre maximum de demandes.")
@click.option("--random", "shuffle", is_flag=True, help="Ordre aléatoire (défaut : envies les plus récentes d'abord).")
@exact_only_option
@dry_run_option
@yes_option
@click.pass_obj
def request_all(
    settings: Settings, limit: int | None, shuffle: bool, exact_only: bool, dry_run: bool, yes: bool
) -> None:
    """Demande en masse les envies absentes de Seerr."""
    candidates = _requestable(_run_pipeline(settings), exact_only)
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
