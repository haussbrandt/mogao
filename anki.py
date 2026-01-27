import asyncio
import json
import os
import time

import requests


class Postprocessor:
    def __init__(self, batch_size=10, timeout=3600) -> None:
        self.batch_size = batch_size
        self.timeout = timeout
        self.last_timer_reset = time.time()
        self.lock = asyncio.Lock()
        self.timer_task = None

    async def check_and_process(self):
        async with self.lock:
            card_ids = call_anki("findCards", query="tag:needs-processing").json()[
                "result"
            ]
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cards waiting for postprocessing: {len(card_ids)}"
            )

            current_time = time.time()
            if len(card_ids) > 0 and self.timer_task is None:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting a new timer")
                self.last_timer_reset = current_time
                self.timer_task = asyncio.create_task(self.start_timer())

            time_since_last = current_time - self.last_timer_reset
            if len(card_ids) >= self.batch_size or (
                len(card_ids) > 0 and time_since_last >= self.timeout
            ):
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting postprocessing")
                await self.run_postprocessing(card_ids)

                self.last_timer_reset = current_time
                if self.timer_task:
                    self.timer_task.cancel()
                self.timer_task = None
            else:
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Not running postprocessing yet, {time_since_last=}"
                )

    async def start_timer(self):
        try:
            await asyncio.sleep(self.timeout)
            await self.check_and_process()
        except asyncio.CancelledError:
            pass

    async def run_postprocessing(self, card_ids):
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
