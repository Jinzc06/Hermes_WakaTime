# wakatime — WakaTime integration for Hermes Agent

Sends **heartbeats** from [Hermes Agent](https://github.com/NousResearch/hermes-agent) to [WakaTime](https://wakatime.com) (or any WakaTime-compatible server such as [wakapi](https://github.com/muety/wakapi) or Hackatime), so the time you spend driving Hermes shows up in your WakaTime dashboard — grouped by **project**, **language**, and **file**, exactly like your editor time.

## Features

- **File-level tracking** — tool calls that touch files (`write_file`, `patch`, `read_file`, `search_files`) produce heartbeats with the real file path, detected language, and write flags.
- **Project auto-detection** — walks up from the file to the nearest git root and uses the repository name; falls back to the working directory. Overridable with `HERMES_WAKATIME_PROJECT`.
- **Full-session coverage** — chat/thinking turns with no tool calls emit `app`-type heartbeats (`Hermes Agent`), so no session is ever invisible.
- **Non-blocking** — heartbeats are queued, deduplicated (max one per entity per minute), batched (up to 20 per request), and sent by a background daemon thread. The agent loop is never blocked.
- **Fail-open** — missing API key? The plugin loads, hooks stay inert, and a one-time warning is logged. No crashes, no errors surfaced to the user.
- **Zero dependencies** — pure Python stdlib (`urllib`, `threading`, `queue`). No pip installs, works on Linux / macOS / Windows.
- **Self-hosted friendly** — point `HERMES_WAKATIME_API_URL` at your own wakapi/Hackatime instance.

## Requirements

- Hermes Agent (any recent version with the plugin system, `hermes plugins` CLI)
- A WakaTime API key from <https://wakatime.com/settings/api-key>

## Installation & enable

The plugin lives at the repository root, so a fresh install is one command:

```bash
hermes plugins install Jinzc06/Hermes_WakaTime --enable
```

(`--enable` skips the interactive "Enable now? [y/N]" prompt.)

Verify it's active:

```bash
hermes plugins list --plain | grep wakatime   # should show "enabled"
```

Or manually — clone the repo and symlink/copy it into your user plugins directory:

```bash
git clone https://github.com/Jinzc06/Hermes_WakaTime.git ~/.hermes/plugins/wakatime
hermes plugins enable wakatime
```

Plugins take effect on the **next session** (restart the app or start a new `hermes` process).

## Configuration

The plugin follows standard WakaTime conventions — in most cases **no
configuration is needed**: if you already use WakaTime in any editor, your API
key is in `~/.wakatime.cfg` and this plugin picks it up automatically.

**API key resolution** (first match wins):

1. `HERMES_WAKATIME_API_KEY` env var
2. `WAKATIME_API_KEY` env var
3. `[settings] api_key` in `~/.wakatime.cfg` (default source)

```ini
# ~/.wakatime.cfg  (standard WakaTime config — created by any WakaTime plugin)
[settings]
api_key = your-api-key-here
```

**Server URL resolution** (for self-hosted wakapi / Hackatime):

1. `HERMES_WAKATIME_API_URL` env var
2. `WAKATIME_API_URL` env var
3. `[settings] api_url` in `~/.wakatime.cfg`

| Variable | Purpose |
|---|---|
| `HERMES_WAKATIME_API_KEY` / `WAKATIME_API_KEY` | API key override (optional — `~/.wakatime.cfg` is the default source) |
| `HERMES_WAKATIME_API_URL` / `WAKATIME_API_URL` | Server override for self-hosted wakapi/Hackatime |
| `HERMES_WAKATIME_CFG` | Path to a custom WakaTime config file (default `~/.wakatime.cfg`; also honors `WAKATIME_HOME`) |
| `HERMES_WAKATIME_PROJECT` | Force a fixed project name for all heartbeats |
| `HERMES_WAKATIME_DEBUG` | Set to `true` for verbose per-send logging |

## How it works

The plugin registers four Hermes hooks:

| Hook | What it does |
|---|---|
| `on_session_start` | Snapshots the working directory (project fallback) and emits an initial heartbeat. |
| `post_tool_call` | Detects file activity from `read_file` / `write_file` / `patch` / `search_files` (including multi-file V4A patches) and `terminal` workdirs; resolves project + language. |
| `post_llm_call` | Emits an app-type heartbeat per turn, covering pure chat/thinking time. |
| `on_session_end` | Synchronously flushes the queue so nothing is lost. |

Heartbeats are POSTed to `POST /api/v1/users/current/heartbeats` using HTTP Basic auth with your API key. A single heartbeat is sent as a JSON object; batches are sent as an array (the WakaTime API accepts both).

**Privacy:** only entity paths, project name, language, category, and a timestamp are transmitted. Prompt text and tool results never leave your machine.

## Testing locally

The repo ships a stdlib mock server that captures heartbeats to `heartbeats.jsonl`, plus a unit test for detection logic:

```bash
python3 tests/mock_wakatime_server.py 18765 test-key &   # terminal 1
HERMES_WAKATIME_API_KEY=test-key \
HERMES_WAKATIME_API_URL=http://127.0.0.1:18765/api/v1/users/current/heartbeats \
hermes chat -q "Create src/hello.py with a greet() function"   # terminal 2
cat tests/heartbeats.jsonl                                    # inspect what was sent
```

```bash
python3 -m unittest discover -s tests -v   # unit tests (no network)
```

## License

[MIT](LICENSE)
