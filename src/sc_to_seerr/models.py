"""Modèles de données partagés entre les services."""

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


@dataclass(frozen=True, slots=True)
class Wish:
    """Une envie de film sur SensCritique."""

    sc_id: int
    title: str
    original_title: str | None
    year: int | None
    release_years: frozenset[int] = field(default_factory=frozenset)
    url: str | None = None

    @property
    def sc_url(self) -> str | None:
        return f"https://www.senscritique.com{self.url}" if self.url else None


@dataclass(frozen=True, slots=True)
class SeerrMovie:
    """Un résultat de recherche Seerr (film TMDB)."""

    tmdb_id: int
    title: str
    original_title: str | None
    year: int | None
    media_status: int | None = None


class MediaStatus(IntEnum):
    """`mediaInfo.status` côté Seerr."""

    UNKNOWN = 1
    PENDING = 2
    PROCESSING = 3
    PARTIALLY_AVAILABLE = 4
    AVAILABLE = 5
    BLOCKLISTED = 6
    DELETED = 7


class RequestStatus(IntEnum):
    """Statut d'une demande Seerr."""

    PENDING = 1
    APPROVED = 2
    DECLINED = 3
    FAILED = 4
    COMPLETED = 5


class WishStatus(StrEnum):
    """Statut final d'une envie, tel qu'affiché dans le rapport."""

    AVAILABLE = "disponible"
    PARTIALLY_AVAILABLE = "partiellement disponible"
    PROCESSING = "en cours"
    PENDING = "demandé"
    DECLINED = "refusé"
    BLOCKLISTED = "blocklisté"
    ABSENT = "absent"
    NOT_FOUND = "non trouvé"


class MatchMethod(StrEnum):
    EXACT = "exact"
    FUZZY = "approché"
    MANUAL = "manuel"
    NONE = "aucun"


@dataclass(slots=True)
class WishResult:
    wish: Wish
    status: WishStatus
    match: MatchMethod
    tmdb_id: int | None = None
    seerr_title: str | None = None
    seerr_year: int | None = None

    @property
    def requestable(self) -> bool:
        return self.status == WishStatus.ABSENT and self.tmdb_id is not None
