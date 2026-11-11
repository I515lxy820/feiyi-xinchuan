from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    model_provider: Literal["mock", "openai"] = "mock"
    model_api_key: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_name: str = ""

    search_provider: Literal["mock", "bocha", "off"] = "mock"
    search_api_key: str = ""
    search_endpoint: str = "https://api.bochaai.com/v1/web-search"

    app_db_path: str = str(BASE_DIR / "data" / "app.db")
    prompt_version: str = "v1"
    model_timeout_seconds: float = 45
    search_timeout_seconds: float = 6
    confidence_threshold: float = 0.72
    web_reader_enabled: bool = True
    web_proxy_url: str = ""
    web_timeout_seconds: float = Field(default=8, gt=0, le=30)
    web_max_pages: int = Field(default=2, ge=1, le=2)
    web_max_bytes: int = Field(default=1_048_576, ge=1024, le=5 * 1024 * 1024)

    @property
    def database_path(self) -> Path:
        path = Path(self.app_db_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def demo_mode(self) -> bool:
        return self.model_provider == "mock" or self.search_provider == "mock"

    @property
    def model_configured(self) -> bool:
        return self.model_provider == "mock" or bool(
            self.model_api_key and self.model_name and self.model_base_url
        )

    @property
    def search_configured(self) -> bool:
        return self.search_provider in {"mock", "off"} or bool(
            self.search_api_key and self.search_endpoint
        )
