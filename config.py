import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

CONFIG_PATH = Path(__file__).with_name("config.toml")


class SettingsModel(BaseModel):
    model_config = ConfigDict(  # pyright: ignore[reportUnannotatedClassAttribute]
        frozen=True,
        extra="forbid",
    )


class AnkiFields(SettingsModel):
    word: str
    pinyin: str
    sentence: str
    meaning: str
    sentence_pinyin: str
    sentence_meaning: str
    word_audio: str
    sentence_audio: str
    sentence_image: str


class AnkiTags(SettingsModel):
    app: str
    needs_processing: str
    needs_audio: str


class AnkiSettings(SettingsModel):
    url: str
    deck: str
    model: str
    fields: AnkiFields
    tags: AnkiTags


class Paths(SettingsModel):
    library: Path
    video_library: Path
    dictionary: Path
    frequencies: Path

    @field_validator("*")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        return CONFIG_PATH.parent / value


class AppSettings(SettingsModel):
    anki: AnkiSettings
    paths: Paths


def load_settings() -> AppSettings:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Required configuration file does not exist: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as config_file:
        return AppSettings.model_validate(tomllib.load(config_file))


settings = load_settings()
