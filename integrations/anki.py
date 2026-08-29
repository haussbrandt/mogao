import asyncio
import base64
import json
import logging
import os
import sys
import time
from functools import lru_cache

import ffmpeg
import requests
from elevenlabs.client import ElevenLabs

from core.config import settings
from core.paths import named_temp_path
from integrations.llm_client import LLMResponseError, generate_json

logger = logging.getLogger(__name__)

ANKI_REQUEST_TIMEOUT_SECONDS = 5

ANKI_MODEL_CSS = """\
.card {
  font-family: arial;
  font-size: 10px;
  text-align: left;
  color: #222222;
  background-color: #fdf6e3;
}
.hanzi {
  font-family: SimSun;
  font-size: 78px;
  text-align: center;
}
.sentence {
  font-family: SimSun;
  font-size: 24px;
  text-align: left;
}
.pinyin {
  font-family: Gentium Plus;
  font-size: 22px;
  color: #005500;
  text-align: center;
}
.pinyinSen {
  font-family: Gentium Plus;
  font-size: 20px;
  color: #005500;
  text-align: left;
}
.english {
  font-family: Georgia;
  font-size: 16px;
}
.meaningSent {
  font-family: Georgia;
  font-size: 16px;
  text-align: left;
}
.description {
  font-family: Georgia;
  font-size: 16px;
  color: #575757;
}
u {
  text-underline-offset: 4px;
  text-decoration-thickness: 3px;
}
"""


class Postprocessor:
    def __init__(self) -> None:
        self.batch_size = settings.postprocessing.text.batch_size
        self.timeout = settings.postprocessing.text.timeout
        self.last_timer_reset = time.time()
        self.lock = asyncio.Lock()
        self.timer_task = None
        self.eleven_client = None

    def initialize_clients(self) -> None:
        if settings.postprocessing.audio.enabled:
            self.eleven_client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])

    async def check_and_process(self):
        async with self.lock:
            call_anki("sync")
            if settings.postprocessing.text.enabled:
                processing_card_ids = call_anki(
                    "findCards", query=f"tag:{settings.anki.tags.needs_processing}"
                ).json()["result"]
                logger.info(
                    f"Cards waiting for postprocessing: {len(processing_card_ids)}"
                )

                current_time = time.time()
                if len(processing_card_ids) > 0 and self.timer_task is None:
                    logger.info("Starting a new postprocessing timer")
                    self.last_timer_reset = current_time
                    self.timer_task = asyncio.create_task(self.start_timer())

                time_since_last = current_time - self.last_timer_reset
                if len(processing_card_ids) >= self.batch_size or (
                    len(processing_card_ids) > 0 and time_since_last >= self.timeout
                ):
                    logger.info("Starting card postprocessing")
                    await self.run_text_postprocessing(processing_card_ids)

                    self.last_timer_reset = current_time
                    if self.timer_task:
                        self.timer_task.cancel()

                    processing_card_ids = call_anki(
                        "findCards",
                        query=f"tag:{settings.anki.tags.needs_processing}",
                    ).json()["result"]

                    if processing_card_ids:
                        logger.info(
                            "Cards still waiting for postprocessing: "
                            f"{len(processing_card_ids)}"
                        )
                        logger.info("Starting a new postprocessing timer")
                        self.timer_task = asyncio.create_task(self.start_timer())
                    else:
                        self.timer_task = None
                else:
                    logger.info(
                        "Not running postprocessing yet; "
                        f"{time_since_last:.1f} seconds since last run"
                    )

            if settings.postprocessing.audio.enabled:
                audio_card_ids = call_anki(
                    "findCards", query=f"tag:{settings.anki.tags.needs_audio}"
                ).json()["result"]
                logger.info(f"Cards waiting for audio: {len(audio_card_ids)}")

                await self.run_audio_postprocessing(audio_card_ids)

    async def start_timer(self):
        try:
            await asyncio.sleep(self.timeout)
            await self.check_and_process()
        except asyncio.CancelledError:
            pass

    async def run_text_postprocessing(self, card_ids):
        cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
        batch_data = []
        for card in cards:
            batch_data.append(
                {
                    "id": card["note"],
                    "source_text": card["fields"][settings.anki.fields.sentence][
                        "value"
                    ],
                    "source_word": card["fields"][settings.anki.fields.word]["value"],
                }
            )
        api_key = os.environ["POSTPROCESSING_API_KEY"]
        results = await call_llm_batch(api_key, batch_data)
        if results:
            for result in results:
                try:
                    note_id = result["id"]

                    fields = {
                        settings.anki.fields.sentence: result["formatted_sentence"],
                        settings.anki.fields.sentence_pinyin: result["sentence_pinyin"],
                        settings.anki.fields.sentence_meaning: result[
                            "sentence_meaning"
                        ],
                    }

                    call_anki(
                        "updateNoteFields", note={"id": note_id, "fields": fields}
                    )
                    call_anki(
                        "removeTags",
                        notes=[note_id],
                        tags=settings.anki.tags.needs_processing,
                    )
                    logger.info(f"Processed Anki note {note_id}")
                except Exception:
                    logger.exception("Failed to postprocess an Anki note")
            call_anki("sync")

    @lru_cache(maxsize=20)
    def call_elevenlabs_api(self, sentence_clean):
        if self.eleven_client is None:
            raise RuntimeError("ElevenLabs client has not been initialized")
        response = self.eleven_client.text_to_speech.convert_with_timestamps(
            voice_id=settings.postprocessing.audio.voice_id, text=sentence_clean
        )
        return response

    async def run_audio_postprocessing(self, card_ids):
        cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
        for card in cards:
            try:
                note_id = card["note"]
                source_text = card["fields"][settings.anki.fields.sentence]["value"]
                source_word = card["fields"][settings.anki.fields.word]["value"]
                logger.info(f"Adding audio to Anki note {note_id}")
                sentence_clean = source_text.replace("<u>", "").replace("</u>", "")
                response = self.call_elevenlabs_api(sentence_clean)
                audio64 = response.audio_base_64

                sentence = "".join(response.alignment.characters)
                start_index = sentence.index(source_word)
                end_index = start_index + len(source_word) - 1

                start_sec = response.alignment.character_start_times_seconds[
                    start_index
                ]
                end_sec = response.alignment.character_end_times_seconds[end_index]

                sentence_audio_path = named_temp_path(f"{note_id}.mp3")
                word_audio_path = named_temp_path(f"{source_word}.mp3")
                with open(sentence_audio_path, "wb") as f:
                    f.write(base64.b64decode(audio64))

                input_file = ffmpeg.input(
                    str(sentence_audio_path), ss=start_sec, to=end_sec
                )
                output = ffmpeg.output(
                    input_file, str(word_audio_path)
                ).overwrite_output()
                ffmpeg.run(output, quiet=True)

                call_anki(
                    "updateNoteFields",
                    note={
                        "id": note_id,
                        "fields": {},
                        "audio": [
                            {
                                "path": str(sentence_audio_path),
                                "filename": f"{note_id}.mp3",
                                "fields": [settings.anki.fields.sentence_audio],
                            },
                            {
                                "path": str(word_audio_path),
                                "filename": f"{source_word}.mp3",
                                "fields": [settings.anki.fields.word_audio],
                            },
                        ],
                    },
                )
                call_anki(
                    "removeTags",
                    notes=[note_id],
                    tags=settings.anki.tags.needs_audio,
                )
                logger.info(f"Added audio to Anki note {note_id}")
                if os.path.exists(word_audio_path):
                    os.remove(word_audio_path)
                if os.path.exists(sentence_audio_path):
                    os.remove(sentence_audio_path)
            except Exception:
                logger.exception(
                    f"Failed to add audio to Anki note {card.get('note', 'unknown')}"
                )

        call_anki("sync")


_POSTPROCESSING_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "sentence_meaning": {"type": "string"},
                    "sentence_pinyin": {"type": "string"},
                    "formatted_sentence": {"type": "string"},
                },
                "required": [
                    "id",
                    "sentence_meaning",
                    "sentence_pinyin",
                    "formatted_sentence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


async def call_llm_batch(api_key, batch_data):
    # TODO: This prompt is kind of dumb, but it works
    prompt_template = 'You are an expert Chinese language tutor. Analyze the following sentence and provide its English meaning, pinyin transcription and underline each occurence of the word using <u> and </u>. The sentence is: "{source_text}"\nThe word is "{source_word}"'
    system_prompt = (
        f"You are a helpful assistant. Process the following list of items.\n"
        f"For each item, apply this logic: {prompt_template}\n\n"
        f"Input Data (JSON): {json.dumps(batch_data, ensure_ascii=False)}"
    )
    try:
        result = await generate_json(
            base_url=settings.postprocessing.text.base_url,
            api_key=api_key,
            model=settings.postprocessing.text.llm,
            prompt=system_prompt,
            response_schema=_POSTPROCESSING_RESPONSE_SCHEMA,
            schema_name="postprocessed_cards",
            timeout=60,
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("results"), list
        ):
            raise LLMResponseError("LLM response did not contain a results array")
        return result["results"]
    except Exception:
        logger.exception("LLM API request failed")
        return []


def call_anki(action, *, request_timeout=None, **params):
    return requests.post(
        settings.anki.url,
        json={"action": action, "params": params, "version": 6},
        timeout=request_timeout,
    )


def ensure_anki_available() -> None:
    try:
        response = call_anki("version", request_timeout=ANKI_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(
            "Anki is enabled, but AnkiConnect is not available at "
            f"{settings.anki.url}. Start Anki with AnkiConnect installed or disable "
            "Anki in config.toml."
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Anki is enabled, but AnkiConnect returned an invalid response"
        )

    if payload.get("error") is not None or payload.get("result") is None:
        raise RuntimeError(
            "Anki is enabled, but AnkiConnect returned an invalid response: "
            f"{payload.get('error') or payload!r}"
        )


def validate_anki_configuration() -> None:
    deck_names = _get_anki_result("deckNames")
    if not isinstance(deck_names, list):
        raise RuntimeError("AnkiConnect returned an invalid deck list")

    model_names = _get_anki_result("modelNames")
    if not isinstance(model_names, list):
        raise RuntimeError("AnkiConnect returned an invalid model list")

    deck_missing = settings.anki.deck not in deck_names
    model_missing = settings.anki.model not in model_names

    if not model_missing:
        _validate_anki_model_fields()

    if not deck_missing and not model_missing:
        return

    missing_resources = []
    if deck_missing:
        missing_resources.append(f"Deck: {settings.anki.deck}")
    if model_missing:
        missing_resources.append(f"Note type: {settings.anki.model}")

    if not _confirm_anki_resource_creation(missing_resources):
        formatted_resources = ", ".join(missing_resources)
        raise RuntimeError(
            f"Anki configuration is missing {formatted_resources}. Create the "
            "missing resources in Anki or set anki.deck, anki.model, and "
            "anki.fields correctly in config.toml."
        )

    if model_missing:
        _create_anki_model()
    if deck_missing:
        _get_anki_result("createDeck", deck=settings.anki.deck)

    _validate_created_anki_resources(deck_missing, model_missing)


def _validate_anki_model_fields() -> None:
    model_fields = _get_anki_result("modelFieldNames", modelName=settings.anki.model)
    if not isinstance(model_fields, list):
        raise RuntimeError(
            f"AnkiConnect returned an invalid field list for model: "
            f"{settings.anki.model}"
        )

    missing_fields = {
        config_name: field_name
        for config_name, field_name in settings.anki.fields.model_dump().items()
        if field_name not in model_fields
    }
    if missing_fields:
        formatted_fields = ", ".join(
            f"{config_name}={field_name!r}"
            for config_name, field_name in missing_fields.items()
        )
        raise RuntimeError(
            f"Anki model {settings.anki.model!r} is missing configured fields: "
            f"{formatted_fields}"
        )


def _confirm_anki_resource_creation(missing_resources: list[str]) -> bool:
    if not sys.stdin.isatty():
        return False

    print("The following Anki resources are missing:")
    for resource in missing_resources:
        print(f"- {resource}")

    try:
        response = input("Create them now? [y/N] ")
    except EOFError:
        return False
    return response.strip().lower() in {"y", "yes"}


def _create_anki_model() -> None:
    configured_fields = list(settings.anki.fields.model_dump().values())
    _get_anki_result(
        "createModel",
        modelName=settings.anki.model,
        inOrderFields=configured_fields,
        css=ANKI_MODEL_CSS,
        isCloze=False,
        cardTemplates=[
            {
                "Name": "Card 1",
                "Front": _anki_model_front_template(),
                "Back": _anki_model_back_template(),
            }
        ],
    )


def _anki_model_front_template() -> str:
    fields = settings.anki.fields
    return f"""\
<div class=hanzi>{_anki_field(fields.word)}</div>
<span style="font-family:SimSun; font-size: 22px; color: #B80000"></span>
<div class=pinyin><br></div>
<div class=english><br></div>
<div class=description><br></div>
<hr>
<div class=sentence>{_anki_field(fields.sentence)}</div>"""


def _anki_model_back_template() -> str:
    fields = settings.anki.fields
    return f"""\
<div class=hanzi>{_anki_field(fields.word)}</div>
<span style="font-family:SimSun; font-size: 22px; color: #B80000;"></span>
<div class=pinyin>{_anki_field(fields.pinyin)}</div>
<div class=english>{_anki_field(fields.meaning)}</div>
<hr>
<div class=sentence>{_anki_field(fields.sentence)}</div>
<div class=pinyinSen>{_anki_field(fields.sentence_pinyin)}</div>
<div class=meaningSent>{_anki_field(fields.sentence_meaning)}</div>

{_anki_field(fields.word_audio)}
{_anki_field(fields.sentence_audio)}

<br>
{_anki_field(fields.sentence_image)}"""


def _anki_field(field_name: str) -> str:
    return "{{" + field_name + "}}"


def _validate_created_anki_resources(
    deck_was_missing: bool, model_was_missing: bool
) -> None:
    if deck_was_missing:
        deck_names = _get_anki_result("deckNames")
        if not isinstance(deck_names, list) or settings.anki.deck not in deck_names:
            raise RuntimeError(
                f"Failed to create configured Anki deck: {settings.anki.deck}"
            )

    if model_was_missing:
        model_names = _get_anki_result("modelNames")
        if not isinstance(model_names, list) or settings.anki.model not in model_names:
            raise RuntimeError(
                f"Failed to create configured Anki model: {settings.anki.model}"
            )
        _validate_anki_model_fields()


def _get_anki_result(action: str, **params):
    try:
        response = call_anki(
            action, request_timeout=ANKI_REQUEST_TIMEOUT_SECONDS, **params
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(f"AnkiConnect action {action!r} failed") from error

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"AnkiConnect action {action!r} returned an invalid response"
        )
    if payload.get("error") is not None:
        raise RuntimeError(
            f"AnkiConnect action {action!r} failed: {payload['error']}"
        )
    if "result" not in payload:
        raise RuntimeError(
            f"AnkiConnect action {action!r} returned an invalid response"
        )
    return payload["result"]


def get_all_words_from_anki_deck(deck_name: str, field_name: str) -> set[str]:
    """
    Sends requests to AnkiConnect to get all cards from the deck `deck_name` and returns a set of values of the field `field_name` from them
    """
    call_anki("sync")
    card_ids = call_anki("findCards", query=f'deck:"{deck_name}"').json()["result"]
    cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
    words = {card["fields"][field_name]["value"] for card in cards}
    return words
