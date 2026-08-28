# Self-hosted transcription server ("serve mode")

Lia can send audio to a **WhisperLive** server you run yourself, instead of
transcribing on the machine you dictate from. This is the setup to use when
you have one box with a GPU and several machines (a laptop, a work desktop)
that should all get fast local-quality transcription without each downloading
models or owning a GPU.

Everything here stays on hardware you control - no third-party service is
involved.

## When this is worth it

| Situation | Better option |
|---|---|
| One machine, has a GPU | Just use the local backend. No server needed. |
| One machine, CPU only | Local Parakeet (English) is realtime on CPU; for Hebrew use Groq's free cloud tier or accept slower local Whisper. |
| Several machines, one GPU box | **This guide.** |
| Machines you cannot install anything on | This guide, tunnel variant. |

## What you need

- A machine with an NVIDIA GPU that stays on (the "server").
- Docker on that machine.
- A way for clients to reach it: a private network (a mesh VPN like Tailscale
  is the least painful) or, for locked-down clients, an HTTPS tunnel.

## 1. Run WhisperLive on the server

[WhisperLive](https://github.com/collabora/WhisperLive) is a WebSocket
front-end for faster-whisper. Point it at whichever model matches your
language - for Hebrew, an [ivrit.ai](https://huggingface.co/ivrit-ai)
fine-tune; for English, `large-v3-turbo`.

```bash
docker run -d --name whisperlive --gpus all -p 9090:9090 \
  ghcr.io/collabora/whisperlive-gpu@sha256:fb95a19a7cb13839d074d6f94d3e546bfc92014a9f4dee24f153de208fe456cc
```

The image is pinned to a digest (that is `latest` as of 2026-08-28) so a
re-pull cannot silently change what runs on your GPU box. To upgrade
deliberately: `docker pull ghcr.io/collabora/whisperlive-gpu:latest`, read the
release notes, then update the digest here from
`docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/collabora/whisperlive-gpu:latest`.

Consult the WhisperLive README for the flags that select and preload a
specific model - a preloaded model is what makes the first request fast.
Verify the server is up before touching Lia:

```bash
curl -i http://localhost:9090
```

One server serves one model at a time. If you dictate in two languages, run a
second container on another port, or let Lia fall back to a different backend
for the other language (see step 3).

## 2. Reach the server from your clients

**Private mesh VPN (recommended).** Install the same mesh VPN on the server
and every client, and use the server's VPN address:

```
ws://<server-vpn-address>:9090
```

Nothing is exposed to the public internet, and the address stays stable when
the server changes networks.

Lia enforces this split: plaintext `ws://` is accepted only for private
addresses (loopback, RFC1918, the 100.64.0.0/10 mesh-VPN range, `.local` /
`.ts.net` names). A `ws://` URL pointing at a public host is refused - the
token and your audio would cross the internet unencrypted - unless you
knowingly set `remote_allow_insecure_ws: true` in the config. Use `wss://`
through a tunnel instead (next section).

**HTTPS tunnel (for clients where you cannot install a VPN client).** Put a
tunnel in front of the server and a reverse proxy that requires a bearer
token, then use:

```
wss://<your-hostname>
```

Do not expose WhisperLive directly - it has no authentication of its own. The
token gate belongs in the proxy (nginx, Caddy, Traefik: any of them can check
an `Authorization` header before proxying the WebSocket upgrade).

## 3. Point Lia at it

Settings -> **Keys & Server** -> Home Server:

- **Server URL**: `ws://host:9090` or `wss://your-hostname`
- **Token**: only if your proxy requires one

Then Settings -> **Models** -> set your dictation (and, if you want, meeting)
model to the remote server.

If the server is unreachable, Lia falls back automatically - to cloud if a key
is configured, otherwise to a local model. A dictation is never lost because
the server was asleep.

## Notes and limits

- **One language per server.** The remote backend serves whatever model the
  container loaded. Utterances in another language should be routed elsewhere
  (Lia's bilingual routing handles this: keep a local or cloud engine selected
  for the other language).
- **Long recordings are chunked.** Lia splits long audio at silence before
  sending, so meeting-length recordings work over the same connection.
- **Latency is network-bound.** On a LAN or a good mesh VPN link this is
  indistinguishable from local. Over a slow uplink it is not.
- **The server sees your audio.** That is the point - make sure it is a
  machine you actually control.
