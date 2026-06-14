# Mogao

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
