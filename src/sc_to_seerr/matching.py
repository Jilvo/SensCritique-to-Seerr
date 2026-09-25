"""Rapprochement d'une envie SensCritique avec un film TMDB (résultats de recherche Seerr)."""

import re
import unicodedata
from difflib import SequenceMatcher

from sc_to_seerr.models import MatchMethod, SeerrMovie, Wish

FUZZY_THRESHOLD = 0.85

_ARTICLES = re.compile(r"^(the|a|an|le|la|les|l|un|une|der|die|das|el|los|las|il|lo)\s+")


def normalize_title(title: str) -> str:
    """Minuscules, sans accents, sans ponctuation ni article initial."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("œ", "oe").replace("æ", "ae")
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\bet\b", "and", text)  # "Tigre et Dragon" == "Tigre & Dragon"
    text = re.sub(r"\s+", " ", text).strip()
    return _ARTICLES.sub("", text)


def year_distance(wish: Wish, movie: SeerrMovie) -> int | None:
    """Plus petit écart entre l'année TMDB et les années connues côté SensCritique."""
    years = wish.release_years or ({wish.year} if wish.year else set())
    if movie.year is None or not years:
        return None
    return min(abs(movie.year - y) for y in years)


_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "vi": 6, "vii": 7, "viii": 8, "ix": 9}


def _numbers(titles: set[str]) -> set[int]:
    """Numéros présents dans les titres ("2", "02", "2eme", "II" -> 2)."""
    found: set[int] = set()
    for title in titles:
        for word in title.split():
            if word in _ROMAN:
                found.add(_ROMAN[word])
            elif match := re.match(r"\d+", word):
                found.add(int(match.group()))
    return found


_NEUTRAL_WORDS = {"vol", "volume", "part", "partie", "chapter", "chapitre", "episode", "movie", "film", "the"}


def _fuzzy_is_plausible(wish_titles: set[str], movie_titles: set[str]) -> bool:
    """Garde-fous d'une correspondance approchée, côté TMDB seulement (SC ajoute
    souvent un numéro ou un sous-titre FR absent de TMDB : "Death Note 2: The Last Name") :
    - pas de numéro TMDB absent côté SC ("Paranormal Activity" != "Paranormal Activity 2") ;
    - le titre TMDB n'est pas le titre SC avec des mots significatifs en plus
      ("Frankenstein" != "I, Frankenstein", mais "Gardiens 3" == "Gardiens Vol. 3").
    """
    if not _numbers(movie_titles) <= _numbers(wish_titles):
        return False
    for w in wish_titles:
        for m in movie_titles:
            wish_words, movie_words = set(w.split()), set(m.split())
            if wish_words < movie_words and not movie_words - wish_words <= _NEUTRAL_WORDS:
                return False
    return True


def _similarity(wish_titles: set[str], movie_titles: set[str]) -> float:
    return max(
        (SequenceMatcher(None, a, b).ratio() for a in wish_titles for b in movie_titles),
        default=0.0,
    )


def find_match(
    wish: Wish, candidates: list[SeerrMovie], year_tolerance: int = 1
) -> tuple[SeerrMovie | None, MatchMethod]:
    """Choisit le meilleur candidat, ou `None` si aucun n'est assez sûr.

    - exact : titre (FR ou original) identique après normalisation, année compatible ;
    - approché : titre très proche (>= FUZZY_THRESHOLD) et année compatible.
    Un titre identique bénéficie d'une année de tolérance supplémentaire
    (dates de sortie FR/festival décalées) ; une année inconnue d'un côté
    n'est acceptée que pour un titre identique.
    """
    wish_titles = {normalize_title(t) for t in (wish.title, wish.original_title) if t}
    scored: list[tuple[float, int, SeerrMovie]] = []
    for movie in candidates:
        movie_titles = {normalize_title(t) for t in (movie.title, movie.original_title) if t}
        similarity = _similarity(wish_titles, movie_titles)
        exact = similarity == 1.0
        if not exact and not _fuzzy_is_plausible(wish_titles, movie_titles):
            continue
        distance = year_distance(wish, movie)
        if distance is None:
            if not exact:
                continue
            distance = year_tolerance + 2  # départage : moins bon qu'une année confirmée
        elif distance > year_tolerance + (1 if exact else 0):
            continue
        if similarity >= FUZZY_THRESHOLD:
            scored.append((similarity, distance, movie))

    if not scored:
        return None, MatchMethod.NONE
    similarity, _, best = max(scored, key=lambda s: (s[0], -s[1]))
    return best, MatchMethod.EXACT if similarity == 1.0 else MatchMethod.FUZZY


_SUBTITLE_SEPARATOR = re.compile(r"\s+[-–—:]\s+|:\s+")
_QUOTES = re.compile(r"[«»“”\"]")


def search_queries(wish: Wish) -> list[str]:
    """Requêtes de recherche à essayer, dans l'ordre.

    Titres complets d'abord (original puis FR), puis leur partie avant un
    sous-titre ("Saga - Partie 2 : ...") : la recherche TMDB échoue souvent
    quand le sous-titre diffère légèrement.
    """
    titles = [
        re.sub(r"\s+", " ", _QUOTES.sub("", t)).strip()
        for t in (wish.original_title, wish.title)
        if t
    ]
    prefixes = [_SUBTITLE_SEPARATOR.split(t, maxsplit=1)[0].strip() for t in titles]
    queries = titles + [p for p in prefixes if len(p) >= 3]
    return list(dict.fromkeys(queries))
