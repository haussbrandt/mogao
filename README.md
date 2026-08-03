# Mogao

## Dictionary setup

Mogao works best with a popup dictionary, but dictionary data is not bundled with this repository.

I recommend using [CC-CEDICT](https://www.mdbg.net/chinese/dictionary?page=cedict), but any dictionary in CC-CEDICT format should work, too.

Download the `.zip` version. The path to the unzipped file should match the path specified under the `dictionary` field in the `[paths]` section of the `config.toml`.

Mogao also supports optional frequency dictionaries.
I recommend using the ones you can find [here](https://zenith-raincoat-5cf.notion.site/Yomitan-Setup-TTS-f454d76706834716bf93919b89145e57) under the name `zhfreq_lists.zip`, but others following the same format should work, too. You can add as many of them as you want - Mogao will calculate the harmonic mean. They should form a nested structure inside the directory specified under the `frequencies` field in the `[paths]` section of the `config.toml`.

You can achieve this by running the following commands after downloading both files:
```shell
mkdir -p dicts/freqs
unzip path/to/cedict_1_0_ts_utf-8_mdbg.zip -d dicts
unzip -q path/to/zhfreq_lists.zip -d dicts/freqs && for archive in dicts/freqs/*.zip; do unzip -q "$archive" -d "${archive%.zip}"; done
```

Run the following command from the repository root. It reads the configured files and generated `static/dict.json`:
```shell
uv run build_dictionary.py
```

## Video URL configuration

Video routes are mounted internally under `/video`. By default, the generated
video links also use `/video`, so the video library is available directly at:

```text
http://localhost:8123/video/
```

To expose the video library at the root of a separate domain, configure the
reverse proxy to add the internal `/video` prefix and set
`MOGAO_VIDEO_BASE_PATH` to an empty string:

```dotenv
MOGAO_VIDEO_BASE_PATH=
```

For example, with Caddy:

```caddyfile
video.example.com {
	rewrite * /video{path}
	reverse_proxy localhost:8123
}
```

To expose it under another public prefix, set that prefix instead:

```dotenv
MOGAO_VIDEO_BASE_PATH=/videos
```
