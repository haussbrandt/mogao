import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

CONFIG_PATH = Path(__file__).with_name("config.toml")


class SettingsModel(BaseModel):
    model_config = ConfigDict(
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


class AnkiSettings(SettingsModel):
    url: str
    deck: str
    model: str
    fields: AnkiFields


class AppSettings(SettingsModel):
    anki: AnkiSettings


def load_settings() -> AppSettings:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Required configuration file does not exist: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as config_file:
        return AppSettings.model_validate(tomllib.load(config_file))


settings = load_settings()
