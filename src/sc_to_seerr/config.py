"""Configuration chargée depuis les variables d'environnement / le fichier .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SensCritique
    sc_username: str
    sc_graphql_url: HttpUrl = HttpUrl("https://apollo.senscritique.com/")
    sc_page_size: int = Field(default=500, ge=1, le=500)

    # Seerr
    seerr_url: HttpUrl
    seerr_api_key: SecretStr
    seerr_language: str = "fr"
    seerr_concurrency: int = Field(default=5, ge=1, le=20)
    seerr_search_pages: int = Field(default=3, ge=1, le=10)

    # Réseau
    http_timeout: float = 30.0
    http_retries: int = Field(default=3, ge=0)

    # Rapprochement
    match_year_tolerance: int = Field(default=1, ge=0)

    # Sorties
    log_file: Path = Path("logs/sc_to_seerr.log")
    log_level: str = "INFO"
    csv_output: Path = Path("output/wishlist_status.csv")
    csv_output_tv: Path = Path("output/series_status.csv")
    cache_file: Path = Path("data/matches.json")
    cache_file_tv: Path = Path("data/matches_tv.json")

    @property
    def seerr_api_base(self) -> str:
        return f"{str(self.seerr_url).rstrip('/')}/api/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
