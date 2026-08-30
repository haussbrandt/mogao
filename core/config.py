import tomllib
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    field_validator,
    model_validator,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.toml"


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


class AnkiKnownSource(SettingsModel):
    decks: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    field: str

    @model_validator(mode="after")
    def validate_has_filter(self):
        if not self.decks and not self.tags:
            raise ValueError("a known-word source requires decks, tags, or both")
        return self


class AnkiSettings(SettingsModel):
    enabled: bool
    url: str
    deck: str
    model: str
    fields: AnkiFields
    tags: AnkiTags
    known_sources: tuple[AnkiKnownSource, ...] = ()


class Paths(SettingsModel):
    library: Path
    video_library: Path

    @field_validator("*")
    @classmethod
    def resolve_path(cls, value: Path) -> Path:
        if value.is_absolute():
            return value
        return CONFIG_PATH.parent / value


class VideoSettings(SettingsModel):
    enabled: bool


class PostprocessingText(SettingsModel):
    enabled: bool
    base_url: str
    llm: str
    batch_size: PositiveInt
    timeout: PositiveInt


class PostprocessingAudio(SettingsModel):
    enabled: bool
    voice_id: str


class Postprocessing(SettingsModel):
    text: PostprocessingText
    audio: PostprocessingAudio


class DictionaryGeneration(SettingsModel):
    enabled: bool
    base_url: str
    llm: str
    chunk_size: PositiveInt
    requests_per_minute: PositiveInt


class AppSettings(SettingsModel):
    anki: AnkiSettings
    paths: Paths
    video: VideoSettings
    postprocessing: Postprocessing
    dictionary_generation: DictionaryGeneration

    @model_validator(mode="after")
    def validate_postprocessing_requires_anki(self):
        enabled_postprocessing = []
        if self.postprocessing.text.enabled:
            enabled_postprocessing.append("postprocessing.text")
        if self.postprocessing.audio.enabled:
            enabled_postprocessing.append("postprocessing.audio")

        if enabled_postprocessing and not self.anki.enabled:
            enabled_sections = ", ".join(enabled_postprocessing)
            raise ValueError(
                f"{enabled_sections} cannot be enabled when anki.enabled is false"
            )
        return self


def load_settings() -> AppSettings:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"Required configuration file does not exist: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as config_file:
        return AppSettings.model_validate(tomllib.load(config_file))


settings = load_settings()
