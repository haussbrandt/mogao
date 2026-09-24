# Mogao

Mogao is a mobile-first EPUB reader and video player for Chinese learners with instant dictionary lookups and Anki flashcard creation.

<p align="center">
  <img
    src="docs/assets/screenshot_popup.jpg"
    alt="A word selected in the ebook reader with its dictionary definition displayed"
    height="360"
  >
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img
    src="docs/assets/screenshot_video_player.png"
    alt="Watching a video with interactive Chinese subtitles"
    height="360"
  >
</p>

## Features

- 📖 EPUB reader
- 📺 Video player with subtitles under the video or in the sidebar
- 🔍 Instantaneous dictionary lookups with just a tap
- 🧠 Seamless Anki flashcard creation
- ✨ Effortless flashcard enrichment with pinyin, translation and audio
- 🧩 Automatic i+1 sentence detection with the new word highlighted
- 📚 Custom dictionary generation per book/video for rare and specific words
- 📱 Designed for use on phones and tablets

Currently, only Simplified Chinese characters are supported. Traditional Chinese character support will be added in the future.

## Quickstart

Install the [prerequisites](#prerequisites), clone the repository and run the quickstart script:

```shell
git clone https://github.com/haussbrandt/mogao.git
cd mogao
./quickstart.sh
```

The quickstart script will handle some of the required things, but there are still some steps you may need to perform manually (like set up the API keys, modify the Anki configuration or disable features you don't want to use). The script will print a list of these steps at the end.

By default, Mogao will create a new note type and deck for you. You can modify config.toml to use a preexisiting one or change the name or field names of the new one.

## Design philosophy

Mogao was created with the intention of making a tool specifically tailored to my needs. That's why you won't find a settings page with themes, fonts and margin sizes, or support for Japanese here. If there is a choice to be made, it's already been made and it's the only way to use the app (barring a few config settings before startup). And if these choices are the same ones you'd make, Mogao is perfect for you. The only choice left is the most important one: which book to read.

The target devices are the ones I use: an Android phone with Chrome for reading books and an iPad with Safari for watching videos, and while Mogao works on other devices such as PCs too, it's not trying to optimize the experience for them.

## Installation and configuration

### Prerequisites

Mogao is based on Python 3.12+ and it's the only hard dependency of this project. To use all the features, there are also these optional dependencies:
- Anki + AnkiConnect - necessary for creating flashcards
- FFmpeg/ffprobe - necessary for the audio and video features

The recommended setup also uses:
- uv - the recommended way to manage Python dependencies
- Caddy or another proxy - HTTPS deployment

### Config files

Mogao reads the configuration options from two files that need to be present in its root directory: `.env` for secrets and `config.toml` for other settings. Copy the provided examples to get started:

```shell
cp .env.example .env
cp config.example.toml config.toml
```

#### Generating secrets and API keys

Run the following command to generate the `MOGAO_SESSION_SECRET` value:
```shell
uv run python -c 'import secrets; print(secrets.token_hex(32))'
```

Run the following command to generate the admin password hash using an interactive prompt:
```shell
uv run python -c 'import bcrypt, getpass; p = getpass.getpass("Admin password: "); print(bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode())'
```

Place the outputs in `.env`.

You can also change the username at this point.

Put the API keys to the AI services in the same file. LLM text post-processing
uses `POSTPROCESSING_API_KEY`, while book and video dictionary generation use
`DICTIONARY_GENERATION_API_KEY`.

#### config.toml

You can configure certain features of Mogao like the Anki deck and field names, paths, AI models used etc. in `config.toml`.
See the [complete `config.toml` reference](docs/CONFIG.md) for every available field.

If you don't want to use specific features, you can disable them by setting their `enabled` value to `false`.

If you disable certain features like audio postprocessing or video, some Anki fields will stop being required in the card model. They still have to be specified in the config (they can be empty strings or any value), but they will get ignored.

The text post-processing and dictionary generation sections each accept their
own OpenAI-compatible `base_url` and `llm` model. This allows the two features
to use different providers.

### Dictionary setup

Mogao includes a ready-to-use popup dictionary based on
[CC-CEDICT](https://cc-cedict.org/) with frequency data derived from
[BCC corpus](https://bcc.blcu.edu.cn/) and
[SUBTLEX-CH](https://doi.org/10.1371/journal.pone.0010729).

You can rebuild `static/dict.json` from the latest sources using:

```shell
uv run python -m scripts.build_dictionary
```

The builder downloads the source data, converts the frequency counts to
comparable ranks and generates a single dictionary file. It can also use local
or alternative sources. Run `uv run python -m scripts.build_dictionary --help` for all
options. For example:

```shell
uv run python -m scripts.build_dictionary \
  --cedict path/to/cedict_ts.u8 \
  --without-bcc \
  --without-subtlex \
  --frequency path/to/yomitan-frequency-directory
```

`--frequency` accepts a Yomitan term-metadata JSON file, directory or ZIP and
may be supplied more than once.

## Deployment

Mogao runs on the computer where you start `uv run server.py`. This can be your
everyday PC, a Raspberry Pi, or a remote server. Keep that computer running
whenever you want to use Mogao.

To use a different port, run `uv run server.py --port 9000` and use that port in
the URLs and proxy configuration below.

- **On the same computer:** Open `http://localhost:8123`.
- **On your local network:** Mogao listens on port `8123`, so other devices on
  the same network can open `http://<host-ip>:8123`.
- **Away from home, privately:** One option is [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve),
  which can give Mogao an HTTPS address accessible to devices in your tailnet
  without making it public. This requires Tailscale on the computer running
  Mogao and on the devices you use to access it. The computer running Mogao
  must stay online.
- **On a remote server:** Put Mogao behind an HTTPS reverse proxy such as Caddy,
  forwarding requests to port `8123`. Configure access to the server and its
  firewall for your deployment.

If you enable Anki features, the computer running Mogao must be able to reach
the AnkiConnect URL set in `config.toml`.

## License

Mogao is licensed under the [MIT license](LICENSE). Portions of the code are derived from third-party projects; see [Third-Party Notices](THIRD_PARTY_NOTICES).
