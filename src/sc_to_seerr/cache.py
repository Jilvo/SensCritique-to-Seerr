"""Cache local des correspondances SensCritique -> TMDB (fichier JSON).

Évite de relancer une recherche Seerr pour chaque envie à chaque exécution.
Une entrée dont `method` vaut "manuel" est une correction faite à la main :
elle n'est jamais écrasée. Pour forcer une correspondance, ajouter par exemple :

    "54807815": {"tmdb_id": 1078605, "title": "Évanouis", "year": 2025, "method": "manuel"}
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from sc_to_seerr.models import MatchMethod

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CachedMatch:
    tmdb_id: int
    title: str
    year: int | None
    method: str

    @property
    def is_manual(self) -> bool:
        return self.method == MatchMethod.MANUAL


class MatchCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[int, CachedMatch] = {}
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._entries = {int(sc_id): CachedMatch(**entry) for sc_id, entry in raw.items()}
        logger.info("Cache : %s correspondances chargées depuis %s", len(self._entries), self.path)

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {str(k): asdict(v) for k, v in sorted(self._entries.items())}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False
        logger.info("Cache : %s correspondances enregistrées dans %s", len(self._entries), self.path)

    def get(self, sc_id: int) -> CachedMatch | None:
        return self._entries.get(sc_id)

    def set(self, sc_id: int, match: CachedMatch) -> None:
        current = self._entries.get(sc_id)
        if current and current.is_manual:
            return
        self._entries[sc_id] = match
        self._dirty = True

    def reset(self) -> None:
        """Repart d'un cache vide (corrections manuelles comprises)."""
        self._entries = {}
        self._dirty = True
        logger.info("Cache : remise à zéro de %s", self.path)

    def clear_automatic(self) -> None:
        """Oublie les correspondances automatiques (garde les manuelles)."""
        self._entries = {k: v for k, v in self._entries.items() if v.is_manual}
        self._dirty = True

    def __len__(self) -> int:
        return len(self._entries)
