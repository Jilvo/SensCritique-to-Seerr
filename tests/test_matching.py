from sc_to_seerr.matching import find_match, normalize_title, search_queries
from sc_to_seerr.models import MatchMethod, SeerrMovie, Wish


def wish(title, original=None, year=None, years=()):
    return Wish(sc_id=1, title=title, original_title=original, year=year, release_years=frozenset(years or ([year] if year else [])))


def movie(tmdb_id, title, original=None, year=None):
    return SeerrMovie(tmdb_id=tmdb_id, title=title, original_title=original, year=year)


def test_normalize_title():
    assert normalize_title("Évanouis") == "evanouis"
    assert normalize_title("The Lord of the Rings: The Fellowship") == "lord of the rings the fellowship"
    assert normalize_title("L'Été dernier") == "ete dernier"
    assert normalize_title("Fast & Furious") == "fast and furious"
    assert normalize_title("  Le   Parrain !") == "parrain"
    assert normalize_title("Tigre et Dragon") == normalize_title("Tigre & Dragon") == "tigre and dragon"
    assert normalize_title("Seven") == "seven"  # "et" n'est remplacé que comme mot entier


def test_exact_match_on_french_title():
    w = wish("Évanouis", "Weapons", 2025)
    candidates = [movie(2, "Évanouis dans la nuit", "Svaniti nella notte", 2024), movie(1, "Évanouis", "Weapons", 2025)]
    best, method = find_match(w, candidates)
    assert best.tmdb_id == 1
    assert method == MatchMethod.EXACT


def test_year_disambiguates_remakes():
    w = wish("Dune", year=2021)
    candidates = [movie(841, "Dune", year=1984), movie(438631, "Dune", year=2021)]
    assert find_match(w, candidates)[0].tmdb_id == 438631


def test_year_tolerance():
    w = wish("Les Gardiens de la Galaxie 3", year=2020)  # titre approché : tolérance simple
    close = "Les Gardiens de la Galaxie Vol. 3"
    assert find_match(w, [movie(1, close, year=2021)])[0] is not None
    assert find_match(w, [movie(1, close, year=2022)])[0] is None
    assert find_match(w, [movie(1, close, year=2022)], year_tolerance=2)[0] is not None


def test_exact_title_gets_one_extra_year_of_tolerance():
    w = wish("Tucker & Dale vs. Evil", year=2012)
    assert find_match(w, [movie(1, "Tucker and Dale vs. Evil", year=2010)])[0] is not None
    assert find_match(w, [movie(1, "Tucker and Dale vs. Evil", year=2009)])[0] is None
    assert find_match(wish("Tucker & Dale vs Evill", year=2012), [movie(1, "Tucker and Dale vs. Evil", year=2010)])[0] is None


def test_any_known_release_year_counts():
    # festival en 2019, sortie en 2021 : TMDB peut indiquer l'une ou l'autre
    w = wish("Titane", year=2019, years=(2019, 2021))
    assert find_match(w, [movie(1, "Titane", year=2021)])[0] is not None


def test_fuzzy_match():
    w = wish("Spider-Man : Across the Spider-Verse", year=2023)
    best, method = find_match(w, [movie(1, "Spider-Man: Across the Spider-Verse", year=2023)])
    assert best is not None
    w = wish("Les Gardiens de la Galaxie Vol 3", year=2023)
    best, method = find_match(w, [movie(1, "Les Gardiens de la Galaxie Vol. 3", year=2023)])
    assert best is not None


def test_fuzzy_match_rejected_when_too_different():
    w = wish("Alien", year=1979)
    assert find_match(w, [movie(1, "Aliens", year=1986), movie(2, "Alien Romulus", year=1979)])[0] is None


def test_fuzzy_rejects_different_sequel_number():
    assert find_match(wish("Paranormal Activity", year=2009), [movie(1, "Paranormal Activity 2", year=2010)])[0] is None
    assert find_match(wish("Crows Zero 2", year=2009), [movie(1, "Crows Zero II", year=2009)])[0] is not None
    assert find_match(wish("Zoolander No. 2", year=2016), [movie(1, "Zoolander 2", year=2016)])[0] is not None
    assert find_match(wish("Baby Cart 1 : Le Sabre", year=1972), [movie(1, "Baby Cart Vol.01 : Le Sabre", year=1972)])[0] is not None


def test_fuzzy_rejects_tmdb_title_with_extra_words():
    assert find_match(wish("Frankenstein", year=2015), [movie(1, "I, Frankenstein", year=2014)])[0] is None
    # l'inverse (sous-titre FR ajouté côté SC) reste accepté
    w = wish("Ip Man - La Légende du grand maître", "Yip Man", 2010)
    assert find_match(w, [movie(1, "Ip Man", "Yip Man", 2009)])[0] is not None


def test_unknown_year_requires_exact_title():
    w = wish("Projet inédit")
    assert find_match(w, [movie(1, "Projet inédit", year=2027)])[1] == MatchMethod.EXACT
    assert find_match(w, [movie(1, "Projet inedits", year=2027)])[0] is None


def test_confirmed_year_beats_unknown_year():
    w = wish("Nosferatu", year=2024)
    candidates = [movie(1, "Nosferatu", year=None), movie(2, "Nosferatu", year=2024)]
    assert find_match(w, candidates)[0].tmdb_id == 2


def test_no_candidates():
    assert find_match(wish("X", year=2000), []) == (None, MatchMethod.NONE)


def test_search_queries_original_first_without_duplicates():
    assert search_queries(wish("Évanouis", "Weapons")) == ["Weapons", "Évanouis"]
    assert search_queries(wish("Dune")) == ["Dune"]


def test_search_queries_strip_quotes():
    assert search_queries(wish("« Il » est revenu", "It")) == ["It", "Il est revenu"]


def test_search_queries_fall_back_to_title_before_subtitle():
    w = wish("La Bataille De Gaulle - Partie 2 : J'écris ton nom")
    assert search_queries(w) == [w.title, "La Bataille De Gaulle"]
    assert search_queries(wish("Spider-Man: No Way Home"))[-1] == "Spider-Man"
    assert search_queries(wish("Spider-Man")) == ["Spider-Man"]  # tiret sans espaces : pas un sous-titre


def test_subtitle_variant_matches_fuzzily():
    w = wish("La Bataille De Gaulle - Partie 2 : J'écris ton nom", year=2026)
    candidates = [
        movie(1, "La bataille de Gaulle : L'âge de fer", year=2026),
        movie(2, "La Bataille de Gaulle : J’écris ton nom", year=2026),
    ]
    assert find_match(w, candidates) == (candidates[1], MatchMethod.FUZZY)
