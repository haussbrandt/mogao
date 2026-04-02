import asyncio
import base64
from functools import lru_cache
import json
import os
import time

import ffmpeg
import requests
from elevenlabs.client import ElevenLabs


class Postprocessor:
    def __init__(self, batch_size=5, timeout=3600) -> None:
        self.batch_size = batch_size
        self.timeout = timeout
        self.last_timer_reset = time.time()
        self.lock = asyncio.Lock()
        self.timer_task = None
        self.eleven_client = ElevenLabs(api_key=os.environ.get("ELEVENLABS_API_KEY"))

    async def check_and_process(self):
        async with self.lock:
            call_anki("sync")
            processing_card_ids = call_anki(
                "findCards", query="tag:needs-processing"
            ).json()["result"]
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cards waiting for postprocessing: {len(processing_card_ids)}"
            )

            audio_card_ids = call_anki("findCards", query="tag:needs-audio").json()[
                "result"
            ]
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cards waiting for audio: {len(audio_card_ids)}"
            )

            current_time = time.time()
            if len(processing_card_ids) > 0 and self.timer_task is None:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting a new timer")
                self.last_timer_reset = current_time
                self.timer_task = asyncio.create_task(self.start_timer())

            time_since_last = current_time - self.last_timer_reset
            if len(processing_card_ids) >= self.batch_size or (
                len(processing_card_ids) > 0 and time_since_last >= self.timeout
            ):
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting postprocessing")
                await self.run_gemini_postprocessing(processing_card_ids)

                self.last_timer_reset = current_time
                if self.timer_task:
                    self.timer_task.cancel()
                self.timer_task = None
            else:
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Not running postprocessing yet, {time_since_last=}"
                )

            # Add audio every time - it's not possible to batch it for lower usage
            await self.run_audio_postprocessing(audio_card_ids)

    async def start_timer(self):
        try:
            await asyncio.sleep(self.timeout)
            await self.check_and_process()
        except asyncio.CancelledError:
            pass

    async def run_gemini_postprocessing(self, card_ids):
        cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
        batch_data = []
        for card in cards:
            batch_data.append(
                {
                    "id": card["note"],
                    "source_text": card["fields"]["SentenceSimplified"]["value"],
                    "source_word": card["fields"]["Simplified"]["value"],
                }
            )
        api_key = os.environ.get("GEMINI_API_KEY")
        results = call_gemini_batch(api_key, batch_data)
        if results:
            for result in results:
                try:
                    note_id = result["id"]

                    fields = {
                        "SentenceSimplified": result["formatted_sentence"],
                        "SentencePinyin.1": result["sentence_pinyin"],
                        "SentenceMeaning": result["sentence_meaning"],
                    }

                    call_anki(
                        "updateNoteFields", note={"id": note_id, "fields": fields}
                    )
                    call_anki(
                        "removeTags",
                        notes=[note_id],
                        tags="needs-processing",
                    )
                    print(f"processed {note_id}")
                except:
                    pass
            call_anki("sync")

    @lru_cache(maxsize=20)
    def call_elevenlabs_api(self, sentence_clean):
        response = self.eleven_client.text_to_speech.convert_with_timestamps(
            voice_id="pFZP5JQG7iQjIQuC4Bku", text=sentence_clean
        )
        return response

    async def run_audio_postprocessing(self, card_ids):
        cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
        for card in cards:
            try:
                note_id = card["note"]
                source_text = card["fields"]["SentenceSimplified"]["value"]
                source_word = card["fields"]["Simplified"]["value"]
                print(f"adding audio to {note_id}")
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

                os.makedirs("/tmp/mogao", exist_ok=True)
                with open(f"/tmp/mogao/{note_id}.mp3", "wb") as f:
                    f.write(base64.b64decode(audio64))

                input_file = ffmpeg.input(
                    f"/tmp/mogao/{note_id}.mp3", ss=start_sec, to=end_sec
                )
                output = ffmpeg.output(
                    input_file, f"/tmp/mogao/{source_word}.mp3"
                ).overwrite_output()
                ffmpeg.run(output, quiet=True)

                call_anki(
                    "updateNoteFields",
                    note={
                        "id": note_id,
                        "fields": {},
                        "audio": [
                            {
                                "path": f"/tmp/mogao/{note_id}.mp3",
                                "filename": f"{note_id}.mp3",
                                "fields": ["SentenceAudio"],
                            },
                            {
                                "path": f"/tmp/mogao/{source_word}.mp3",
                                "filename": f"{source_word}.mp3",
                                "fields": ["Audio"],
                            },
                        ],
                    },
                )
                call_anki(
                    "removeTags",
                    notes=[note_id],
                    tags="needs-audio",
                )
                print(f"added audio to {note_id}")
                if os.path.exists(f"/tmp/mogao/{source_word}.mp3"):
                    os.remove(f"/tmp/mogao/{source_word}.mp3")
                if os.path.exists(f"/tmp/mogao/{note_id}.mp3"):
                    os.remove(f"/tmp/mogao/{note_id}.mp3")
            except:
                pass

        call_anki("sync")


def call_gemini_batch(api_key, batch_data):
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"

    # TODO: This prompt is kind of dumb, but it works
    prompt_template = 'You are an expert Chinese language tutor. Analyze the following sentence and provide its English meaning, pinyin transcription and underline each occurence of the word using <u> and </u>. The sentence is: "{source_text}"\nThe word is "{source_word}"'
    system_prompt = (
        f"You are a helpful assistant. Process the following list of items.\n"
        f"For each item, apply this logic: {prompt_template}\n\n"
        f"Input Data (JSON): {json.dumps(batch_data, ensure_ascii=False)}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": system_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {"type": "INTEGER"},
                        "sentence_meaning": {"type": "STRING"},
                        "sentence_pinyin": {"type": "STRING"},
                        "formatted_sentence": {"type": "STRING"},
                    },
                    "required": [
                        "id",
                        "sentence_meaning",
                        "sentence_pinyin",
                        "formatted_sentence",
                    ],
                },
            },
        },
    }
    try:
        response = requests.post(api_url, json=payload, timeout=60)
        response.raise_for_status()

        result_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(result_text)
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return []


def call_anki(action, **params):
    return requests.post(
        "http://localhost:8765", json={"action": action, "params": params, "version": 6}
    )


def get_all_words_from_anki_deck(deck_name: str, field_name: str) -> set[str]:
    """
    Sends requests to AnkiConnect to get all cards from the deck `deck_name` and returns a set of values of the field `field_name` from them
    """
    call_anki("sync")
    card_ids = call_anki("findCards", query=f'deck:"{deck_name}"').json()["result"]
    cards = call_anki("cardsInfo", cards=card_ids).json()["result"]
    words = {card["fields"][field_name]["value"] for card in cards}
    return words
