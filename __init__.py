"""wakatime — WakaTime heartbeats for Hermes Agent.

Sends coding-activity heartbeats to wakatime.com (or any WakaTime-compatible
server such as wakapi / Hackatime) so time spent driving Hermes Agent shows up
in your WakaTime dashboard, grouped by project, language, and file — exactly
like your editor time.

Activation is handled by the Hermes plugin system: ``hermes plugins enable
wakatime``. At runtime the plugin requires a WakaTime API key; without one the
hooks are inert (fail-open, like the bundled langfuse plugin) and a one-time
warning is logged.

API key resolution (first match wins):
  1. HERMES_WAKATIME_API_KEY env var
  2. WAKATIME_API_KEY env var (standard, shared with all WakaTime plugins)
  3. [settings] api_key in ~/.wakatime.cfg — the standard WakaTime config
     file created by every official WakaTime plugin/CLI (usually already
     present, so no configuration is needed)

Server URL resolution (first match wins):
  1. HERMES_WAKATIME_API_URL env var
  2. WAKATIME_API_URL env var
  3. [settings] api_url in ~/.wakatime.cfg (self-hosted wakapi/Hackatime)

Optional env vars:
  HERMES_WAKATIME_CFG      - path to a custom WakaTime config file
                             (default: ~/.wakatime.cfg; also honors WAKATIME_HOME)
  HERMES_WAKATIME_PROJECT  - force a project name (default: auto-detected)
  HERMES_WAKATIME_DEBUG    - "true" for verbose logging

Design notes
------------
* Pure stdlib (urllib, threading, queue, json) — zero pip dependencies, so the
  plugin installs anywhere ``hermes plugins install <owner>/wakatime`` runs.
* Heartbeats are queued, deduplicated (max one per entity per minute), batched
  (up to 20 per POST), and sent by a background daemon thread — the agent loop
  is never blocked. On session end the queue is flushed synchronously, bounded
  by the POST timeout.
* Nothing sensitive is ever transmitted: only entity paths, project name,
  language, category, and a timestamp. Prompt/tool content never leaves the
  machine.
"""

from __future__ import annotations

import base64
import configparser
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_VERSION = "1.1.0"
_USER_AGENT = f"hermes-wakatime/{PLUGIN_VERSION}"
_DEFAULT_API_URL = "https://api.wakatime.com/api/v1/users/current/heartbeats"
_MIN_HEARTBEAT_INTERVAL = 60.0  # throttle: seconds between heartbeats per entity
_MAX_QUEUE_SIZE = 200           # drop-new when full (bounded memory)
_BATCH_SIZE = 20                # max heartbeats per POST request
_POST_TIMEOUT = 6.0             # seconds; bounds blocking on session-end flush
_MAX_THROTTLE_ENTRIES = 4096    # cap on the (project, entity) dedup map
_MAX_PROJECT_CACHE = 4096       # cap on the directory -> project cache

_APP_ENTITY = "Hermes Agent"  # entity used for non-file activity (type: "app")
_FILE_TOOLS = {"read_file", "write_file", "patch", "search_files"}
_WRITE_TOOLS = {"write_file", "patch"}
# V4A multi-file patch header: "*** Update File: <path>"
_PATCH_HEADER_RE = re.compile(r"^\s*\*\*\*\s*Update File:\s*(.+?)\s*$", re.MULTILINE)
_MAX_PATCH_TARGETS = 10

# ---------------------------------------------------------------------------
# WakaTime language names (must match wakatime.com's canonical names)
# ---------------------------------------------------------------------------

_LANGUAGES: Dict[str, str] = {
    ".py": "Python", ".pyi": "Python", ".pyw": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JSX",
    ".ts": "TypeScript", ".mts": "TypeScript", ".cts": "TypeScript", ".tsx": "TSX",
    ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Shell",
    ".md": "Markdown", ".markdown": "Markdown", ".rst": "reStructuredText", ".txt": "Text",
    ".json": "JSON", ".jsonc": "JSON (with Comments)",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".html": "HTML", ".htm": "HTML", ".xml": "XML",
    ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".sql": "SQL", ".ipynb": "Jupyter Notebook",
    ".lua": "Lua", ".pl": "Perl", ".r": "R", ".dart": "Dart",
    ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell",
    ".clj": "Clojure", ".tf": "Terraform", ".proto": "Protocol Buffers",
    ".vue": "Vue", ".svelte": "Svelte", ".dockerfile": "Dockerfile",
    ".graphql": "GraphQL",
}

_FILENAME_LANGUAGES = {
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
    "cmakelists.txt": "CMake",
    "justfile": "Just",
}

_WRITING_LANGUAGES = {"Markdown", "Text", "reStructuredText"}

# ---------------------------------------------------------------------------
# Module state (thread-safe; one process shares all concurrent sessions)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_sender_lock = threading.Lock()          # serializes POSTs from any flusher
_pending: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
_last_sent: Dict[Tuple[str, str], float] = {}  # (project, entity) -> last emit ts
_project_cache: Dict[str, str] = {}             # directory -> project name
_sender_started = False
_sender_thread: Optional[threading.Thread] = None
_failure_count = 0
_last_failure_log = 0.0
_warned_no_key = False
_session_cwd: Optional[str] = None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _debug_enabled() -> bool:
    return _env("HERMES_WAKATIME_DEBUG").lower() in {"1", "true", "yes", "on"}


def _debug(message: str, *args: Any) -> None:
    if _debug_enabled():
        logger.info("wakatime: " + message, *args)


def _cfg_path() -> str:
    """Path to the WakaTime config file (standard: ~/.wakatime.cfg)."""
    override = _env("HERMES_WAKATIME_CFG")
    if override:
        return os.path.expanduser(override)
    home = _env("WAKATIME_HOME")
    if home:
        return os.path.join(os.path.expanduser(home), ".wakatime.cfg")
    return os.path.join(os.path.expanduser("~"), ".wakatime.cfg")


_cfg_cache: Dict[str, Any] = {"key": None, "settings": {}}


def _read_cfg() -> Dict[str, str]:
    """Parse ``[settings]`` from the WakaTime config file (mtime-cached).

    configparser lowercases option names, so look up ``api_key`` / ``api_url``.
    A missing or unparseable file yields {}.
    """
    path = _cfg_path()
    try:
        st = os.stat(path)
        cache_key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = (path, None, None)
    if _cfg_cache["key"] == cache_key:
        return _cfg_cache["settings"]
    settings: Dict[str, str] = {}
    if cache_key[1] is not None:
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(path, encoding="utf-8")
            if parser.has_section("settings"):
                settings = {k: v.strip() for k, v in parser.items("settings")}
        except Exception as exc:
            _debug("failed to parse %s: %s", path, exc)
    _cfg_cache["key"] = cache_key
    _cfg_cache["settings"] = settings
    return settings


def _api_key() -> str:
    for name in ("HERMES_WAKATIME_API_KEY", "WAKATIME_API_KEY"):
        value = _env(name)
        if value:
            return value
    return _read_cfg().get("api_key", "")


def _api_url() -> str:
    for name in ("HERMES_WAKATIME_API_URL", "WAKATIME_API_URL"):
        value = _env(name)
        if value:
            return value
    return _read_cfg().get("api_url") or _DEFAULT_API_URL


def _warn_no_key() -> None:
    """Log the missing-key warning exactly once per process."""
    global _warned_no_key
    if _warned_no_key:
        return
    _warned_no_key = True
    logger.warning(
        "wakatime plugin: no API key found. Set HERMES_WAKATIME_API_KEY / "
        "WAKATIME_API_KEY, or add an api_key under [settings] in "
        "~/.wakatime.cfg. Heartbeats are disabled until a key is configured."
    )


# ---------------------------------------------------------------------------
# Project detection
# ---------------------------------------------------------------------------

def _cache_project(directory: str, name: str) -> None:
    with _state_lock:
        if len(_project_cache) >= _MAX_PROJECT_CACHE:
            _project_cache.clear()
        _project_cache[directory] = name


def _project_from_dir(directory: str) -> str:
    """Resolve a project name for *directory*.

    Walks up (max 12 levels) looking for a ``.git`` marker and uses the
    repository's directory name; otherwise falls back to the directory's own
    name. Results are cached per directory.
    """
    with _state_lock:
        cached = _project_cache.get(directory)
    if cached:
        return cached
    try:
        cur = Path(directory)
        for _ in range(12):
            if (cur / ".git").exists():
                name = cur.name or "hermes"
                _cache_project(directory, name)
                return name
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:
        pass
    name = Path(directory).name or "hermes"
    _cache_project(directory, name)
    return name


def _project_for(path: Optional[str] = None) -> str:
    """Project name for a file path (or the process working directory)."""
    override = _env("HERMES_WAKATIME_PROJECT")
    if override:
        return override
    if path:
        try:
            p = Path(os.path.abspath(os.path.expanduser(path)))
            start = p if p.is_dir() else p.parent
            return _project_from_dir(str(start))
        except Exception:
            pass
    try:
        cwd = _session_cwd or os.getcwd()
    except Exception:
        cwd = os.path.expanduser("~")
    return _project_from_dir(cwd)


# ---------------------------------------------------------------------------
# Language / category detection
# ---------------------------------------------------------------------------

def _language_for(path: str) -> Optional[str]:
    try:
        name = Path(path).name.lower()
        if name in _FILENAME_LANGUAGES:
            return _FILENAME_LANGUAGES[name]
        return _LANGUAGES.get(Path(path).suffix.lower())
    except Exception:
        return None


def _category_for(language: Optional[str]) -> str:
    return "writing" if language in _WRITING_LANGUAGES else "coding"


# ---------------------------------------------------------------------------
# Heartbeat engine
# ---------------------------------------------------------------------------

def _emit(
    entity: str,
    *,
    type_: str = "file",
    project: Optional[str] = None,
    language: Optional[str] = None,
    is_write: bool = False,
    category: str = "coding",
) -> bool:
    """Queue one heartbeat if it passes the per-entity throttle. Never raises."""
    if not _api_key():
        _warn_no_key()
        return False
    if not entity:
        return False
    project = project or _project_for(None)
    now = time.time()

    throttle_key = (project, entity)
    with _state_lock:
        last = _last_sent.get(throttle_key)
        if last is not None and (now - last) < _MIN_HEARTBEAT_INTERVAL:
            return False
        _last_sent[throttle_key] = now
        if len(_last_sent) > _MAX_THROTTLE_ENTRIES:
            # Drop the oldest half to bound memory.
            cutoff = sorted(_last_sent.values())[len(_last_sent) // 2]
            for key in [k for k, v in _last_sent.items() if v <= cutoff]:
                del _last_sent[key]

    heartbeat: Dict[str, Any] = {
        "entity": entity,
        "type": type_,
        "time": now,
        "project": project,
    }
    if language:
        heartbeat["language"] = language
    if is_write:
        heartbeat["is_write"] = True
    if category and category != "coding":
        heartbeat["category"] = category

    try:
        _pending.put_nowait(heartbeat)
    except queue.Full:
        return False
    _ensure_sender()
    return True


def _emit_app(project: Optional[str] = None) -> None:
    """Non-file activity (chat/thinking turns, terminal workdirs)."""
    _emit(_APP_ENTITY, type_="app", project=project or _project_for(None), category="coding")


def _emit_file(tool_name: str, path: str) -> None:
    try:
        entity = os.path.abspath(os.path.expanduser(path))
    except Exception:
        entity = path
    language = _language_for(entity)
    _emit(
        entity,
        type_="file",
        project=_project_for(entity),
        language=language,
        is_write=tool_name in _WRITE_TOOLS,
        category=_category_for(language),
    )


def _emit_patch_targets(args: Dict[str, Any]) -> None:
    patch_text = args.get("patch")
    if not isinstance(patch_text, str) or not patch_text:
        return
    seen: set = set()
    for match in _PATCH_HEADER_RE.finditer(patch_text):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            _emit_file("patch", target)
        if len(seen) >= _MAX_PATCH_TARGETS:
            break


# ---------------------------------------------------------------------------
# Sender thread + synchronous flush
# ---------------------------------------------------------------------------

def _ensure_sender() -> None:
    global _sender_started, _sender_thread
    with _state_lock:
        if _sender_started:
            return
        _sender_started = True
        _sender_thread = threading.Thread(
            target=_sender_loop, name="wakatime-sender", daemon=True
        )
        _sender_thread.start()


def _sender_loop() -> None:
    while True:
        try:
            first = _pending.get(timeout=60.0)
        except queue.Empty:
            continue
        batch = [first]
        try:
            while len(batch) < _BATCH_SIZE:
                batch.append(_pending.get_nowait())
        except queue.Empty:
            pass
        _post_batch(batch)


def _flush_now() -> None:
    """Drain the queue and POST synchronously (session end)."""
    items: list = []
    while True:
        try:
            items.append(_pending.get_nowait())
        except queue.Empty:
            break
    if items:
        _post_batch(items)


def _auth_encodings(api_key: str) -> list:
    """Both Basic encodings seen in the wild: <key> and <key>: (username only)."""
    return [
        base64.b64encode(api_key.encode("utf-8")).decode("ascii"),
        base64.b64encode((api_key + ":").encode("utf-8")).decode("ascii"),
    ]


def _note_failure(message: str) -> None:
    global _failure_count, _last_failure_log
    _failure_count += 1
    now = time.time()
    if now - _last_failure_log > 300.0:  # at most one warning per 5 minutes
        _last_failure_log = now
        logger.warning(
            "wakatime plugin: %s (%d consecutive failed sends)", message, _failure_count
        )


def _post_batch(heartbeats: list) -> None:
    """POST one batch. Never raises; logs failures with a 5-minute cooldown."""
    api_key = _api_key()
    if not api_key:
        _warn_no_key()
        return

    payload = heartbeats[0] if len(heartbeats) == 1 else heartbeats
    data = json.dumps(payload).encode("utf-8")
    url = _api_url()

    with _sender_lock:
        for attempt, encoded in enumerate(_auth_encodings(api_key)):
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": "Basic " + encoded,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=_POST_TIMEOUT) as resp:
                    status = resp.status
                if status in (200, 201, 202):
                    _failure_count = 0
                    _debug("sent %d heartbeat(s) -> %d", len(heartbeats), status)
                    return
                _note_failure(f"WakaTime API returned HTTP {status}")
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    _debug("Basic auth rejected with <key> encoding, retrying <key:>")
                    continue
                _note_failure(f"WakaTime API returned HTTP {exc.code}")
                return
            except Exception as exc:
                _note_failure(f"heartbeat send failed: {exc}")
                return


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def on_session_start(*, session_id: str = "", **_: Any) -> None:
    global _session_cwd
    try:
        _session_cwd = os.getcwd()
    except Exception:
        _session_cwd = None
    # A session start is activity: capture it even before the first turn ends.
    _emit_app()


def on_post_tool_call(*, tool_name: str = "", args: Any = None, **_: Any) -> None:
    if not isinstance(args, dict):
        return
    if tool_name in _FILE_TOOLS:
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            _emit_file(tool_name, path.strip())
            return
        if tool_name == "patch" and args.get("mode") == "patch":
            _emit_patch_targets(args)
        elif tool_name == "search_files":
            # Explicit directory scoping still counts as project activity.
            if isinstance(path, str) and path.strip() and path.strip() != ".":
                _emit_file(tool_name, path.strip())
    elif tool_name == "terminal":
        workdir = args.get("workdir")
        if isinstance(workdir, str) and workdir.strip():
            _emit_app(_project_for(workdir))


def on_post_llm_call(**_: Any) -> None:
    # Fired once per turn after the tool-calling loop: guarantees activity is
    # tracked even for pure chat/thinking turns with no tool calls.
    _emit_app()


def on_session_end(**_: Any) -> None:
    # Best-effort final flush so nothing queued is lost on session exit.
    try:
        _flush_now()
    except Exception as exc:
        _debug("final flush failed: %s", exc)


def register(ctx) -> None:
    if not _api_key():
        _warn_no_key()
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
