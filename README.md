# anotherme-plugin-diary

Diary entries with AI analysis, timeline, and mood tracking for
[AnotherMe](https://github.com/MatousVavra/AnotherMe).

Extracted from the AnotherMe host repository at commit 9a6ef26 — prior
history lives there.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AI_BASE_URL` | `https://llm.ai.e-infra.cz/v1/` | AI provider base URL (used by the host LLM client via `ctx.llm_client`) |
| `AI_API_KEY` | — | AI provider API key |

Auto-extraction of facts and people is configurable per-install via the
plugin settings UI.

## Development

Unit tests run standalone against `FakePluginContext`:

    pip install fastapi pydantic httpx pyyaml pytest pytest-asyncio openai python-multipart
    pytest tests/ --ignore=tests/integration

Integration tests run inside the released app image (see
`.github/workflows/test.yml`). To develop against a live app, point
`COMMUNITY_PLUGINS_DIR` at this checkout's parent directory.

## Releases

Tag `vX.Y.Z` (must match `plugin/plugin.yaml` `version`), then bump the tag
in the [community index](https://github.com/MatousVavra/anotherme-plugins).
