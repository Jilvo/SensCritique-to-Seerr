# SensCritique → Seerr

Vérifie si les envies de films d'un compte [SensCritique](https://www.senscritique.com) sont disponibles, en cours ou demandées dans [Seerr](https://github.com/seerr-team/seerr) (ex-Overseerr).

`check` est en lecture seule. Les commandes `random` et `request-all` créent des demandes dans Seerr (confirmation demandée, `--dry-run` pour simuler).

## Installation

Python 3.14 et [Poetry](https://python-poetry.org) :

```bash
poetry install
cp .env.example .env   # puis renseigner SC_USERNAME, SEERR_URL, SEERR_API_KEY
```

La liste d'envies SensCritique doit être publique. La clé API Seerr se trouve dans *Paramètres > Général*.

## Utilisation

```bash
poetry run sc-to-seerr ping                  # teste les connexions
poetry run sc-to-seerr check                 # vérifie toutes les envies
poetry run sc-to-seerr check --limit 50      # seulement les 50 plus récentes
poetry run sc-to-seerr check --show all      # détaille tous les statuts
poetry run sc-to-seerr check --show absent --show pending
poetry run sc-to-seerr check --refresh-cache # refait les correspondances automatiques
poetry run sc-to-seerr -v check              # logs détaillés en console
```

### Faire des demandes

Seules les envies **absentes** de Seerr (jamais demandées) sont proposées. Les demandes sont faites au nom de l'utilisateur de la clé API : si c'est un administrateur, elles sont approuvées et envoyées à Radarr directement.

```bash
# Tirage au sort d'une envie : (o)ui pour la demander, (a)utre pour retirer, (q)uitter
poetry run sc-to-seerr random
poetry run sc-to-seerr random --yes          # demande directement le premier tirage

# Demandes en masse (liste affichée puis confirmation)
poetry run sc-to-seerr request-all --dry-run            # simule
poetry run sc-to-seerr request-all --limit 20           # les 20 envies les plus récentes
poetry run sc-to-seerr request-all --limit 20 --random  # 20 au hasard
```

Options communes : `--exact-only` (ignore les correspondances approchées), `--dry-run`, `-y/--yes` (sans confirmation, pour un usage planifié).

### Cache

```bash
poetry run sc-to-seerr rebuild-cache                # repart de zéro (corrections manuelles comprises)
poetry run sc-to-seerr rebuild-cache --keep-manual  # garde les corrections manuelles
```

Sorties :
- console : détail des statuts choisis (`--show`, par défaut absents, refusés et non trouvés) + résumé ;
- CSV (`CSV_OUTPUT`, séparateur `;`, compatible Excel) : une ligne par envie ;
- log (`LOG_FILE`) : tout le déroulé, dont les envies sans correspondance avec leur lien SensCritique.

Statuts : `disponible`, `partiellement disponible`, `en cours` (demande approuvée, téléchargement en cours), `demandé` (en attente d'approbation), `refusé`, `blocklisté`, `absent` (jamais demandé), `non trouvé` (pas de correspondance TMDB).

## Fonctionnement

1. **SensCritique** : les envies de films sont lues via l'API GraphQL du site (pas d'API publique officielle).
2. **Rapprochement** : SensCritique ne fournit pas d'identifiant TMDB/IMDb. Chaque envie est cherchée dans Seerr par titre original puis français (puis sans sous-titre), et acceptée si le titre correspond (exact ou très proche) et que l'année est compatible.
3. **Statut** : tous les médias et demandes de Seerr sont récupérés en quelques appels, puis croisés avec les identifiants TMDB.

Les correspondances sont gardées dans `CACHE_FILE` (`data/matches.json`) : seule la première exécution lance une recherche par envie (~2-3 min pour 3000 films), les suivantes prennent quelques secondes.

### Corriger une correspondance

Ajouter ou modifier l'entrée dans `data/matches.json` avec `"method": "manuel"` ; elle ne sera jamais écrasée, même avec `--refresh-cache` :

```json
"19453495": {"tmdb_id": 414419, "title": "Kill Bill : The Whole Bloody Affair", "year": 2011, "method": "manuel"}
```

La clé est l'identifiant SensCritique (colonne `sc_id` du CSV), `tmdb_id` celui du film sur TMDB.

## Architecture

```
src/sc_to_seerr/
  config.py          configuration (.env, pydantic-settings)
  models.py          modèles partagés (Wish, SeerrMovie, statuts)
  services/
    base.py          client HTTP async commun (httpx, nouvelles tentatives)
    senscritique.py  service SensCritique (GraphQL)
    seerr.py         service Seerr (recherche, médias, demandes)
  matching.py        rapprochement SensCritique -> TMDB
  cache.py           cache JSON des correspondances
  pipeline.py        orchestration et classification des statuts
  requester.py       création des demandes Seerr
  report.py          tableau Rich et export CSV
  logging_setup.py   logs fichier + console
  cli.py             commandes Click
```

## Tests

```bash
poetry run pytest
```
