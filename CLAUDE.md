# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Talk-Screen-AI-Windows is a voice-enabled AI assistant (Gemini-backed) that watches the screen/webcam and talks with the user via microphone and TTS. It's a small, dependency-heavy Python app with **no build system, no tests, and no linter configured** — just two runnable entry points under `src/`.

## Running the app

There is a `.venv` already set up in the repo root; use its interpreter directly rather than assuming an activated shell.

```powershell
# Windows entry point (recommended — this is the actively developed one)
.venv\Scripts\python.exe src\main_windows.py

# Cross-platform / Gemini Live API entry point (older, simpler)
.venv\Scripts\python.exe src\main.py
```

Install/update deps with `.venv\Scripts\pip.exe install -r requirements.txt`.

A `.env` file in the repo root is required (see `SETUP.md` for the full list of variables). At minimum:
```
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.6-flash
```
`src/config/settings.py` calls `load_config()` **at import time** (`config = load_config()` runs when the module loads), so any script importing `src.config.settings` will raise `ValueError` immediately if `GEMINI_API_KEY` is missing — there's no lazy init.

No test suite, linter, or type-checker is configured in this repo.

## Architecture: two independent entry points

The two `main*.py` files are **not shared code** beyond `AudioHandler`/`config`/`logger` — they implement different interaction models and should be treated as separate apps that happen to live in the same `src/` tree.

### `src/main.py` — Gemini Live API version
Uses `client.aio.live.connect(...)` to open one bidirectional streaming session. Five concurrent `asyncio.TaskGroup` tasks talk through queues:
- `send_text()` — reads stdin, sends to the live session
- `process_screen_capture()` — grabs a screen frame every ~1s via `src/screen/screen_capture.py` (`ScreenCapture`, cross-platform via `mss`), pushes to `data_out_queue`
- `process_audio_input()` / `process_audio_output()` — mic capture and speaker playback via `AudioHandler`
- `send_realtime_data()` — drains `audio_out_queue`/`data_out_queue` into `session.send()`
- `receive_ai_response()` — reads streamed audio/text back from the session

All state flows through `asyncio.Queue`s; there's no per-turn request/response — it's one long streaming session until `q` is entered at the `message >` prompt.

### `src/main_windows.py` — Windows-only "persistent room assistant"
Windows-only (asserts `sys.platform == 'win32'` at startup). This is a heavier, stateful rewrite that does **not** use the Live API — instead it calls `client.aio.models.generate_content()` per turn. Key differences from `main.py`:

- **Webcam every turn, screen on demand**: `src/camera/camera_capture_windows.py` (`CameraCaptureWindows`, OpenCV) grabs a frame the instant speech is detected, and it is attached to every turn. The *screen* is only captured when the user asks about it — see "Screen questions" below. The `ScreenCaptureWindows` **class** in `src/screen/screen_capture_windows.py` is still unused dead code; only the module-level `grab_screen_image()` function is live.
- **Voice activity detection — three independent gates.** Loudness alone cannot tell a spoken word from a door slam, so an utterance must pass all three before it costs an API call:
  1. **Level** (`_is_speech_chunk`): RMS above an **adaptive noise floor** (`noise_floor_rms` + `SPEECH_RMS_MARGIN`), not a fixed threshold — a fixed one false-triggers once the room gets noisy. The floor is only updated from *quiet* chunks; letting loud noise raise it would desensitise the mic to the next real utterance.
  2. **Timbre** (`_is_voice_like`): voice-band energy ratio, spectral flatness, and zero-crossing rate. Rejects thuds, hiss, clicks, fans.
  3. **Movement** (`_utterance_is_speech_like`): spectral flux + RMS coefficient of variation across the whole utterance. Rejects sustained tones, chords, alarms, and whines — which are harmonic and in-band, so they pass the timbre gate and *look like a vowel*. Speech constantly moves; a held tone doesn't.
  - Gates 2 and 3 are complementary, and both are needed: measured on real SAPI speech vs. synthetic noise, a chord passes timbre 100% but fails movement, while keyboard clicks pass movement but fail timbre. Thresholds were picked from measured data (speech's worst 1s slice: flux 0.70 / level-var 0.42; worst noise: flux 0.23 / level-var 0.21) — re-measure with `VOICE_MIN_FLUX`/`VOICE_MIN_LEVEL_COV` before changing them.
  - An utterance is only *sent* if it also accumulated `VOICE_MIN_VOICED_CHUNKS` **voiced** chunks. Counting *voiced* chunks (not buffer length) is load-bearing: the silence tail is always appended, so a length check is always satisfied and every cough would cost a full audio+image API call.
- **Second noise gate, after transcription**: the prompt instructs the model to return empty `transcript` *and* `reply` for noise, and `_is_meaningful_transcript()` drops empty/filler/punctuation-only transcripts before anything is spoken. Do not "helpfully" restore a fallback opener here — an earlier prompt said to invent a friendly opener when speech was unclear, which is exactly what made the assistant talk to background noise.
- **One API call per turn**: `_generate_voice_turn()` sends WAV audio + the webcam frame and gets back JSON with `transcript`, `reply`, `visual_note`, and `memory` — transcription, answering, scene description, and memory-writing are deliberately folded into a *single* request to control cost and latency. `_generate_text_turn()` is the typed-input equivalent.
- **Screen vs webcam is a model decision, not a keyword match**: `_screen_rules()` is a prominent, standalone prompt section (not buried in the JSON contract, where it was ignored). It defaults to the *screen* for anything about the user's work and reserves the webcam for the person/room. It also forbids the failure mode where the assistant explains that it *could* look at the screen instead of just looking — that happened live, as did answering "what's wrong?" with generic advice while never looking.
- **Screen questions (two-pass, deliberately)**: because transcript and reply come back from the *same* call, the model cannot ask for a screenshot before answering. So the turn JSON carries a `needs_screen` flag: pass 1 sets it and returns an empty reply, then `_resolve_screen_request()` captures the screen and `_answer_with_screen()` produces the real answer. This is the only path that spends a second API call, and only for turns that need it — attaching a screenshot to every turn would add image tokens and latency to all the ones that don't. `grab_screen_image()` builds its own `mss` instance per call because mss handles are thread-bound and this runs on an `asyncio.to_thread` worker.
  - Privacy note: this sends a screenshot of the desktop to Gemini. Set `SCREEN_CAPTURE_ENABLED=0` to remove the capability (the flag is then omitted from the prompt entirely).
- **Local TTS**: replies are spoken with `pyttsx3` (Windows SAPI) via `_speak_sync`, run on a dedicated `pythoncom`-initialized thread (`asyncio.to_thread`) — SAPI/COM requires this. Rate/voice via `TTS_RATE` / `TTS_VOICE_NAME`.
- **Self-trigger prevention** (subtle, don't regress): `is_speaking` only masks chunks *as they are read*, one per loop iteration — PyAudio keeps buffering the assistant's own voice for the whole reply. So `speak_text` sets `_mic_drain_pending`, and `process_audio_input` calls `_drain_microphone_backlog()` to actually discard that backlog. **Only that loop may drain** — two threads must never read the same PyAudio stream concurrently.
- **Persona: room-mate, not project manager.** This is a deliberate product decision, not an accident of prompting. The assistant builds a long-term picture of *who the user is* and explicitly does **not** track calendars or paper mechanics. Memory once filled with meeting times (seven different "tomorrow" supervisor slots at once) and paper outlines, which made it nag; that is now blocked in three independent places (guard, contract, HARD RULES). Don't "helpfully" re-add deadline tracking.
- **Persistent memory**: `assistant_memory.json` at repo root stores `key_points` (durable facts chosen by the *model* and returned in the turn JSON), `visual_notes`, and `observations`, folded into `dynamic_system_prompt` every turn by `_refresh_dynamic_system_prompt()`.
  - Each fact is `{ts, category, content}`, filed into one of `PROFILE_CATEGORIES` (`identity`, `research`, `personal`, `hobbies`, `life`, `room`, `appearance`). Records with no `category` (written before this existed) degrade to `personal` rather than being lost.
  - `_cap_key_points()` caps **per category** and **pins identity's oldest record**. A single global truncation previously let a chatty week evict the user's own name. Output is re-sorted by `ts` so that when two notes disagree, the later one is read last and wins.
  - `_render_profile_block()` renders identity **first and exempt from `PROFILE_BLOCK_CHARS`** — the name must never be squeezed out by hobby facts. It also returns the still-empty categories, which is what steers questions toward genuine gaps (`_next_follow_up_theme()` prefers categories with <2 facts).
  - `_is_junk_fact()` rejects two families, applied on write **and on load** (so old files self-clean at startup): the user's own questions, and `_VOLATILE_FACT_PATTERN` — dates/times, deadlines, meetings, seminars, to-dos, and paper mechanics (outline/chapter/draft/literature/paper/thesis), in English **and Japanese**. It deliberately over-rejects: a missed hobby gets re-offered next turn, whereas a missed deadline pollutes every prompt until hand-deleted.
  - `config.system_prompt` is **not** injected. Its `settings.py` fallback ("professional and detailed… as thorough an answer as possible") contradicts every brevity rule in the persona. Only an operator-set `SYSTEM_PROMPT` env var is used, via `self.operator_system_prompt`. `src/main.py` still uses the settings default and is unaffected.
- **Proactive behavior**: the assistant speaks first via `proactive_greet_on_speech()`, triggered when VAD hears a genuine voice that *didn't* qualify as a full utterance ("presence without address") — the branch that previously dropped audio silently. Gated by `PROACTIVE_COOLDOWN_SEC` / `PROACTIVE_MIN_IDLE_SEC`.
  - Gate is split in two on purpose: `_proactive_conditions_ok()` excludes the proactive lock, `_proactive_gate_open()` adds it. The coroutine re-checks *conditions* after acquiring the lock — checking the full gate there would make it see its own lock held and bail every time (this was a real bug).
- **Keep-awake**: `ctypes`/`SetThreadExecutionState` with `ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED` (no `ES_DISPLAY_REQUIRED` — it should keep running *with the screen off*). Thread-scoped, so it must stay on the main thread that owns the loop.
- **API quota handling**: `_handle_api_error` backs off for `API_BACKOFF_SEC` (via the `api_blocked_until` timestamp behind the `api_temporarily_blocked` property) instead of latching dead until restart.

### Shared pieces
- `src/config/settings.py` — `AudioConfig`/`GeminiConfig`/`AppConfig` dataclasses, loaded once from env via `python-dotenv`.
- `src/audio/audio_handler.py` — `AudioHandler` wraps `pyaudio` streams; `_select_input_device()` picks the mic via `MIC_DEVICE_INDEX` or `MIC_DEVICE_NAME` env var, falling back to the system default.
- `src/logger.py` — module-level `logger` singleton (`logging.getLogger("TalkScreenAI")`), writes to both stdout and rotating `app.log` (1MB × 5 backups). Import `from src.logger import logger`, don't create new loggers.

All source files are documented in Japanese docstrings/comments; the codebase itself is bilingual (README/SETUP.md have English + Japanese sections). Match the existing style (Japanese docstrings) when editing existing modules; new user-facing strings in `main_windows.py` are increasingly English-only (see `REPLY_STYLES`, `FOLLOW_UP_THEMES`, log messages) — follow the convention of whichever file you're editing.

## Tunables (all optional, read from `.env` at startup)

`main_windows.py` only. Defaults in parentheses.

| Variable | Purpose |
|---|---|
| `SPEECH_RMS_THRESHOLD` (220) / `SPEECH_RMS_MARGIN` (150) | Floor and required margin above ambient noise for a chunk to count as speech |
| `VOICE_ATTACK_CHUNKS` (1) | Speech chunks needed to *start* an utterance. Kept at 1 on purpose: at 3, words beginning with a fricative ("Thanks", "Hello") never start at all |
| `VOICE_BAND_RATIO_MIN` (0.55) / `VOICE_FLATNESS_MAX` (0.40) / `VOICE_ZCR_MAX` (0.30) | Timbre gate: how voice-shaped a chunk must be. Lower the ratio / raise the others to be more permissive |
| `VOICE_MIN_FLUX` (0.40) / `VOICE_MIN_LEVEL_COV` (0.25) | Movement gate: how much the utterance must vary over time. **Lower both if it ignores real speech**; raise if steady tones still trigger it |
| `VOICE_MIN_VOICED_CHUNKS` (2) | Voiced chunks needed to *send* an utterance; below this it's treated as "presence" and may trigger a proactive greeting. **Must stay above `VOICE_ATTACK_CHUNKS`** — `voiced_chunks` starts at the attack count, so equal values make the proactive branch unreachable (the code clamps this for you) |
| `VOICE_SILENCE_CHUNKS` (8) | Trailing silence that ends a turn (~64ms per chunk at 16kHz/1024) |
| `PROACTIVE_COOLDOWN_SEC` (240) / `PROACTIVE_MIN_IDLE_SEC` (150) | How rarely the assistant may open a conversation |
| `TTS_RATE` (185) / `TTS_VOICE_NAME` | SAPI speech rate; substring match against installed voice names |
| `TTS_TAIL_GUARD_SEC` (0.4) | Pause after speaking before re-arming the mic |
| `API_BACKOFF_SEC` (120) | Pause after a quota/429 error |
| `SCREEN_CAPTURE_ENABLED` (1) | Set `0` to disable screenshots entirely |
| `SCREEN_MONITOR` (1) / `SCREEN_MAX_EDGE` (1280) | Which display to grab (0 = all joined), and its downscale size |
| `CAMERA_DEVICE_INDEX` (0), `MIC_DEVICE_INDEX` / `MIC_DEVICE_NAME` | Device selection |

## Gotchas

- Both entry points do `sys.path.append(...)` to the repo root before importing `src.*` — they're meant to be run as scripts (`python src/main_windows.py`), not as an installed package.
- **`.venv\Scripts\python.exe` is a redirector**: launching it spawns the base interpreter (`…\Programs\Python\Python311\python.exe`) as a **child** that does all the work, while the venv process idles at 1 thread. So two `python.exe` entries in Task Manager is **normal for one app** — the real one has ~35 threads and ~180MB. Don't mistake it for two concurrent instances; to stop the app, kill both (`Get-Process python | Stop-Process`).
- `main_windows.py` shuts down on `stop_event` (set by typing `q`), not by `send_text` returning. With no interactive stdin it logs once and runs voice-only — don't "fix" that into a loop, it used to spin on `EOFError` several times a second.
- Never `await queue.put(...)` on `audio_out_queue` from inside the `TaskGroup` body — it's bounded and `process_audio_input` is a competing putter, so a full queue deadlocks shutdown before `__aexit__` runs. Use `put_nowait` + explicit `.cancel()`.
- `src/config/settings.py` executes `load_config()` at import time — importing it anywhere without a valid `.env`/`GEMINI_API_KEY` will throw, including in one-off scripts or REPLs.
- `main_windows.py` hard-exits on non-Windows platforms; don't expect it to run in CI on Linux/macOS.
