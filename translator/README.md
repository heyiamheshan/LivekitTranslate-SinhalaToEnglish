<a href="https://livekit.io/">
  <img src="./.github/assets/livekit-mark.png" alt="LiveKit logo" width="100" height="100">
</a>

# Translator agent (Python)

Per-room LiveKit Agents worker for [Live Translate](../README.md). One process per room, one Gemini Live session per `(speaker → target_lang)` pair, audio + caption text-streams published back into the room.

For the project overview and quick start (which runs this worker alongside the frontend), see the [root README](../README.md). This file covers running, testing, and deploying the agent on its own.

## What it does

Listens to every participant's mic track and watches each participant's `lang` attribute. For each distinct `(speaker, target_lang)` pair where `source_lang != target_lang`, the [`TranslationRouter`](src/router.py) opens a [Gemini Live](https://ai.google.dev/gemini-api/docs/live) session ([`GeminiSession`](src/session.py)) and publishes:

- A translated audio track named `tx:<speaker>:<target_lang>`, which the frontend subscribes to via track-name routing.
- A `lk.translation` text-stream carrying live captions, also tagged with the target language.

Same-language pairs are skipped entirely, so a monolingual room costs nothing.

## Run locally

From this directory:

```bash
uv sync
uv run python src/agent.py dev
```

Or from the repo root, which runs the agent alongside the Next.js frontend:

```bash
pnpm run dev
```

The agent reads `translator/.env.local` for `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `GEMINI_API_KEY`.

## Test and lint

```bash
uv run pytest          # router unit tests
uv run ruff check      # lint
uv run ruff format     # format
```

## Deploy

To [LiveKit Cloud Agents](https://docs.livekit.io/agents/ops/deployment/):

```bash
lk agent create --secrets-file .env.local .   # first time only
lk agent deploy
```

The included `Dockerfile` is also suitable for any container host. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `GEMINI_API_KEY` on the host.

## Working with coding agents

LiveKit ships a CLI and an MCP server for browsing the [Agents docs](https://docs.livekit.io/agents/) — useful when extending this worker. Install the CLI:

- **macOS:** `brew install livekit-cli`
- **Linux:** `curl -sSL https://get.livekit.io/cli | bash`
- **Windows:** `winget install LiveKit.LiveKitCLI`

Then `lk docs search ...` / `lk docs get-page ...`. See the [Using coding agents](https://docs.livekit.io/intro/coding-agents/) guide for details, including MCP setup. This project's [`AGENTS.md`](AGENTS.md) tells coding assistants how to work in this directory.
