"""Restitution des résultats : tableau Rich en console et export CSV."""

import csv
from collections import Counter
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from sc_to_seerr.models import MatchMethod, WishResult, WishStatus

STATUS_STYLE: dict[WishStatus, tuple[str, str]] = {
    WishStatus.AVAILABLE: ("✅", "green"),
    WishStatus.PARTIALLY_AVAILABLE: ("🟡", "yellow"),
    WishStatus.PROCESSING: ("⏬", "cyan"),
    WishStatus.PENDING: ("⏳", "blue"),
    WishStatus.DECLINED: ("⛔", "red"),
    WishStatus.BLOCKLISTED: ("🚫", "red"),
    WishStatus.ABSENT: ("❌", "magenta"),
    WishStatus.NOT_FOUND: ("❓", "bright_black"),
}

CSV_FIELDS = [
    "statut",
    "titre",
    "titre_original",
    "annee",
    "sc_id",
    "sc_url",
    "tmdb_id",
    "titre_seerr",
    "annee_seerr",
    "correspondance",
]


def _label(status: WishStatus) -> str:
    icon, style = STATUS_STYLE[status]
    return f"[{style}]{icon} {status.value}[/{style}]"


def print_summary(console: Console, results: list[WishResult]) -> None:
    counts = Counter(r.status for r in results)
    table = Table(title=f"Résumé — {len(results)} envies de films", show_header=False, min_width=40)
    table.add_column("Statut")
    table.add_column("Nombre", justify="right")
    for status in WishStatus:
        if counts[status]:
            table.add_row(_label(status), str(counts[status]))
    console.print(table)


def print_details(
    console: Console, results: list[WishResult], statuses: set[WishStatus], title: str = "Détail", sort: bool = True
) -> None:
    rows = [r for r in results if r.status in statuses]
    if not rows:
        return
    table = Table(title=f"{title} ({len(rows)})")
    table.add_column("Statut", no_wrap=True)
    table.add_column("Titre SensCritique")
    table.add_column("Année", justify="right")
    table.add_column("Correspondance Seerr")
    table.add_column("TMDB", justify="right")
    order = list(WishStatus)
    if sort:
        rows.sort(key=lambda r: (order.index(r.status), r.wish.title.lower()))
    for r in rows:
        wish_title = escape(r.wish.title)
        if r.wish.original_title:
            wish_title += f" [dim]({escape(r.wish.original_title)})[/dim]"
        seerr = f"{escape(r.seerr_title)} ({r.seerr_year or '?'})" if r.seerr_title else ""
        if r.match == MatchMethod.FUZZY:
            seerr += " [yellow]~[/yellow]"
        table.add_row(_label(r.status), wish_title, str(r.wish.year or ""), seerr, str(r.tmdb_id or ""))
    console.print(table)


def write_csv(path: Path, results: list[WishResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig : BOM pour qu'Excel lise correctement les accents
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";")
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "statut": r.status.value,
                    "titre": r.wish.title,
                    "titre_original": r.wish.original_title or "",
                    "annee": r.wish.year or "",
                    "sc_id": r.wish.sc_id,
                    "sc_url": r.wish.sc_url or "",
                    "tmdb_id": r.tmdb_id or "",
                    "titre_seerr": r.seerr_title or "",
                    "annee_seerr": r.seerr_year or "",
                    "correspondance": r.match.value,
                }
            )
