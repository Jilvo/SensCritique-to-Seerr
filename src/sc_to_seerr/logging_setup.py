"""Logs : tout dans le fichier .log, avertissements et erreurs aussi en console (Rich)."""

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler


def setup_logging(log_file: Path, level: str, console: Console, verbose: bool = False) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level.upper())
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s - %(message)s")
    )

    console_handler = RichHandler(console=console, show_path=False, markup=False)
    console_handler.setLevel(logging.DEBUG if verbose else logging.ERROR)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # httpx logue chaque requête en INFO : on le garde pour le mode DEBUG uniquement
    logging.getLogger("httpx").setLevel(logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
