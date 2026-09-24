# `config.toml` reference

Mogao reads `config.toml` from the project root when it starts. Copy
[`config.example.toml`](../config.example.toml) to create it, then edit the
values for your setup. The quickstart script will do it for you.

Every section and field below must be present, except
`[[anki.known_sources]]` and its optional `decks` and `tags` filters. Unknown
fields are rejected. Use TOML booleans (`true` or `false`), quoted strings,
positive integers where specified, and arrays of quoted strings for filters.

API keys and the session secret belong in `.env`, not `config.toml`; see the
[README](../README.md#generating-secrets-and-api-keys).

## Anki

| Field | Type | Description |
| --- | --- | --- |
| `anki.enabled` | Boolean | Enables creating Anki notes and using Anki cards as known words. When `false`, both postprocessing features must also be disabled. |
| `anki.url` | String | AnkiConnect URL reachable from the computer running Mogao, for example `http://localhost:8765`. |
| `anki.deck` | String | Destination deck for notes created by Mogao. Mogao can offer to create it at startup if it does not exist. |
| `anki.model` | String | Anki note type for those notes. Mogao can offer to create it at startup if it does not exist. |

### Anki note fields

Each `anki.fields.*` value is the **name of a field in the configured Anki note
type**, not the content to put in that field. Match the names in an existing
note type exactly. When Mogao creates a note type, it uses these names.

| Field | Type | Content Mogao puts in the named Anki field |
| --- | --- | --- |
| `anki.fields.word` | String | Selected Chinese word. Also used to read known words from the destination deck. |
| `anki.fields.pinyin` | String | Pinyin for the selected word. |
| `anki.fields.sentence` | String | Sentence containing the selected word. |
| `anki.fields.meaning` | String | Dictionary definitions for the selected word. |
| `anki.fields.sentence_pinyin` | String | Sentence pinyin produced by text postprocessing. |
| `anki.fields.sentence_meaning` | String | Sentence translation produced by text postprocessing. |
| `anki.fields.word_audio` | String | Pronunciation clip of the word produced by audio postprocessing. |
| `anki.fields.sentence_audio` | String | Sentence audio produced by audio postprocessing or cut from a video. |
| `anki.fields.sentence_image` | String | Screenshot captured from a video. |

All nine names must appear in `config.toml`. An existing Anki note type does
not need `word_audio` when audio postprocessing is disabled. It does not need
`sentence_audio` when both audio postprocessing and video are disabled, or
`sentence_image` when video is disabled. If you use an existing note type,
unused names can remain as empty strings in the config.

### Anki tags

| Field | Type | Description |
| --- | --- | --- |
| `anki.tags.app` | String | Tag added to notes created by Mogao. Mogao also adds an item-specific tag using this value as a prefix. |
| `anki.tags.needs_processing` | String | Tag marking notes waiting for text postprocessing; removed after successful processing. |
| `anki.tags.needs_audio` | String | Tag marking notes waiting for audio postprocessing; removed after successful processing. |

### Additional known-word sources

`[[anki.known_sources]]` is an optional, repeatable table (you can specify more than one element). It tells Mogao to
count words in other Anki cards as known in addition to words in `anki.deck`.
It is useful if you have pre-existing decks with cards you already learned outside of Mogao. You can point to them here and Mogao will treat them as known when calculating known words percentage and finding i+1 sentences.
For example:

```toml
[[anki.known_sources]]
decks = ["Mandarin: Vocabulary"]
tags = ["HSK1", "HSK2"]
field = "Simplified"
```

| Field | Type | Description |
| --- | --- | --- |
| `anki.known_sources[].decks` | Array of strings | Optional deck filter. A card may be in **any** listed deck. Missing decks are ignored. |
| `anki.known_sources[].tags` | Array of strings | Optional tag filter. A card may have **any** listed tag. Tags with no matching cards contribute no words. |
| `anki.known_sources[].field` | String | Name of the Anki field containing the word on matching cards. Required for each source. |

Provide at least one nonempty `decks` or `tags` list for each source. If both
are present, a card must match a listed deck **and** a listed tag. You can add
multiple `[[anki.known_sources]]` tables when sources use different word
fields. If every listed deck is missing, that source is skipped. A matching
card without the configured `field` causes an error.

## Storage paths

Relative paths are resolved from the directory containing `config.toml` (the
project root); absolute paths are used as given.

| Field | Type | Description |
| --- | --- | --- |
| `paths.library` | Path string | Directory for imported books, their reading progress, and generated book dictionaries. |
| `paths.video_library` | Path string | Directory for imported videos, their progress, subtitles, and generated video dictionaries. |

## Video

| Field | Type | Description |
| --- | --- | --- |
| `video.enabled` | Boolean | Enables the video player and video import features. |

## Text postprocessing

Text postprocessing enriches Anki notes with sentence pinyin, translation, and
word highlighting. It requires `anki.enabled = true`. When enabled, set
`POSTPROCESSING_API_KEY` in `.env`.

| Field | Type | Description |
| --- | --- | --- |
| `postprocessing.text.enabled` | Boolean | Enables LLM processing of newly created Anki notes. |
| `postprocessing.text.base_url` | String | OpenAI-compatible API base URL. Mogao appends `/chat/completions` unless it is already present. |
| `postprocessing.text.llm` | String | Model identifier sent to that API. |
| `postprocessing.text.batch_size` | Positive integer | Number of waiting cards that triggers a processing batch. |
| `postprocessing.text.timeout` | Positive integer | Maximum wait, in seconds, before processing waiting cards even if the batch size has not been reached. This is a batching timer, not the API request timeout. |

## Audio postprocessing

Audio postprocessing uses ElevenLabs text to speech for sentence and word audio
on Anki notes. It requires `anki.enabled = true`. When enabled, set
`ELEVENLABS_API_KEY` in `.env`.

| Field | Type | Description |
| --- | --- | --- |
| `postprocessing.audio.enabled` | Boolean | Enables generated audio for newly created Anki notes. |
| `postprocessing.audio.voice_id` | String | ElevenLabs voice ID used for speech generation. |

## Dictionary generation

Dictionary generation uses an LLM to create supplemental vocabulary entries
for imported books and, when video is enabled, subtitles. When enabled, set
`DICTIONARY_GENERATION_API_KEY` in `.env`. Its model and API endpoint can differ
from those used for text postprocessing.

| Field | Type | Description |
| --- | --- | --- |
| `dictionary_generation.enabled` | Boolean | Enables supplemental dictionary generation for books and videos. |
| `dictionary_generation.base_url` | String | OpenAI-compatible API base URL. Mogao appends `/chat/completions` unless it is already present. |
| `dictionary_generation.llm` | String | Model identifier sent to that API. |
| `dictionary_generation.chunk_size` | Positive integer | Approximate number of Chinese characters grouped into each book or subtitle request. A single chapter or subtitle line may make a chunk larger. |
| `dictionary_generation.requests_per_minute` | Positive integer | Rate limit for dictionary generation requests, shared across concurrent jobs. |
