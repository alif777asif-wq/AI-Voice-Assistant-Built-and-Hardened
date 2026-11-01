# L

A cross-platform, fully local, offline-capable AI voice assistant. Wake
word -> speech-to-text -> LLM -> text-to-speech, entirely in Python,
developed on Linux Mint and engineered to run identically on Windows 11.
No paid APIs, no proprietary SDKs, no usage-limited free tier anywhere in
the pipeline -- every inference step runs on your own hardware.

```
"Hey [wake word]"  -->  [wake word]  -->  [record + VAD endpoint]  -->  [STT]
                                                                          |
                                                                          v
         [speaker] <-- [overlapped sentence-chunked TTS] <-- [LLM, streamed]
```

Ships with `wake_word.model: "hey_jarvis"` (openWakeWord's pretrained
model) as the default trigger phrase -- see "Known limitations" below
("No custom 'Hey, L' wake word model out of the box") for what it'd
actually take to get a trained "Hey L" model working instead.

## Architecture

```
                         +-------------------------------+
                         |     MicrophoneStream (mic)     |
                         |  sounddevice.InputStream, one   |
                         |  80ms int16 mono block/callback |
                         +----------------+----------------+
                                          |
                     one shared capture stream, whole app lifetime
                                          |
        +---------------+---------------+---------------+
        |               |                               |
        v               v                               v
+---------------+  +--------------+              +----------------+
| WakeWordDetector| | FrameVad /   |              | (idle: frames  |
| openWakeWord,    | | EndpointDet. |              |  ignored while |
| ONNX backend     | | webrtcvad,   |              |  THINKING /    |
| (IDLE state)     | | 20ms votes   |              |  SPEAKING)     |
+---------------+  | (LISTENING   |              +----------------+
                    |  state)      |
                    +------+-------+
                           |  utterance PCM (int16)
                           v
                  +-------------------+
                  |    WhisperSTT     |   faster-whisper / CTranslate2
                  |  (THINKING state) |   offline, no network call
                  +---------+---------+
                            |  transcript text
                            v
                  +-------------------+        +----------------------+
                  | ConversationMemory| <----> | SQLite (rolling       |
                  |                   |        | window + LLM-driven   |
                  +---------+---------+        | compaction summary)   |
                            |  chat messages             +--------------+
                            v
                  +-------------------+
                  |   OllamaClient    |   local Ollama server,
                  | streamed tokens   |   http://localhost:11434
                  +---------+---------+
                            |  text deltas
                            v
                  +-------------------+
                  |  SentenceChunker  |   splits streamed tokens into
                  +---------+---------+   speakable sentences early
                            |
                            v
                  +-------------------+       +--------------------+
                  |     PiperTTS      | ----> |    AudioPlayer     |
                  | (SPEAKING state,  |       | sounddevice.Output |
                  |  overlapped synth |       | Stream, plays each |
                  |  + playback via   |       | sentence as the    |
                  |  SpeechStreamer)  |       | next one synths    |
                  +-------------------+       +--------------------+
```

Full FSM: `IDLE -> LISTENING -> THINKING -> SPEAKING -> IDLE`, implemented
in `src/l_assistant/pipeline/orchestrator.py`.

**Self-hearing (L transcribing its own TTS output as a command) is
handled in three layers**, not one -- see that file's module docstring
for the full reasoning:
1. `SpeechStreamer` mutes the mic (drops frames at the source, in the
   PortAudio callback itself) for the entire duration of every spoken
   utterance -- audio played while muted is never captured at all.
2. `_settle_after_speech` drains, pauses `vad.post_speech_settle_ms`,
   drains again after unmuting -- covers output-buffer latency and room
   reverb tail that outlast the electrical end of playback.
3. `_looks_like_self_echo` discards a transcript that's both very recent
   and textually close to what L just said, as a last resort.

Real acoustic echo cancellation (a reference-signal adaptive filter, as
WebRTC/Speex implement) was evaluated and deliberately not integrated:
the available Python bindings ship no prebuilt wheels for a modern
x86_64 target on either OS -- both need compiling against system C
libraries, which breaks this project's "no compiler required on either
platform" bar every other dependency choice here has held to. This also
means barge-in (interrupting L mid-sentence by talking over it) isn't
implemented -- muting during playback is, if anything, more correct for
that specifically: the mic can't hear an interruption attempt while
muted. `AudioPlayer.stop_immediately()` is the extension point if a real
AEC path is ever added.

Two things layer on top of the diagram above, both off by default: the
LLM can be given tools (launching apps, touching files, etc. -- see
"System control" below) that insert a bounded call-model /
run-a-tool / call-model-again loop into the THINKING stage; and the
whole pipeline can run inside a system tray icon on a background thread
instead of the foreground terminal (see "Running in the background"
below).

## Why these libraries

| Stage | Library | Why | Offline? | License |
|---|---|---|---|---|
| Audio I/O | `sounddevice` (PortAudio) | One API, one code path on both OSes | n/a | MIT |
| VAD / endpointing | `webrtcvad-wheels` | Prebuilt wheels for Linux **and** Windows -- the plain `webrtcvad` package has no Windows wheel on many Python versions and silently falls back to a source build | Yes | BSD |
| Wake word | `openwakeword` (ONNX backend, pinned) | Apache-2.0, offline, no per-query cost or rate limit; ONNX backend forced on both OSes because openWakeWord's *own* default backend differs by OS (TFLite on Linux, ONNX on Windows) | Yes | Apache-2.0 |
| Speech-to-text | `faster-whisper` (CTranslate2, local) or Groq cloud Whisper | Local: ~4x faster than stock Whisper on CPU at equal accuracy. Cloud: zero local CPU cost, for genuinely low-spec hardware | Local: yes. Cloud: no | MIT (faster-whisper) |
| Text-to-speech | `piper-tts` (OHF-Voice/piper1-gpl) | Self-contained wheel with espeak-ng embedded -- no system phonemizer package on either OS | Yes (after first voice download) | **GPL-3.0** (see Licensing) |
| LLM | Local **Ollama** server via `httpx`, or Groq's cloud API -- see below | Official installers for both OSes, zero compilation, zero token limits/rate limits once a model is pulled | Local: yes. Cloud: no | MIT (Ollama itself) |
| Config | `pydantic` + `pyyaml` | Validated schema, one source of truth | n/a | MIT / OSI |
| Paths | `platformdirs` | Correct per-OS data directory without a single `if platform.system()` in application code | n/a | MIT |
| System tray (optional) | `pystray` + `tendo` + `Pillow` | Cross-platform tray icon + single-instance lock; imported lazily so terminal-mode use never needs them | n/a | LGPLv3 (pystray) / PSF (tendo) / MIT-CMU (Pillow) |

Nothing above requires a compiler, Visual Studio Build Tools, or a CUDA
toolkit on either platform for the default CPU configuration.

## Latency

Actual numbers depend entirely on your CPU (and GPU, if you enable
`use_cuda`/Ollama GPU offload) -- there's no honest single number to
quote. Every turn logs its own breakdown, so you can measure your own
hardware directly:

```
Turn timing: stt=420ms  time-to-first-token=310ms  total=1840ms
```

Rough shape on a mid-range 2023-era laptop CPU, no GPU, `small.en` +
`llama3.2:3b` + `en_US-lessac-medium`:

- Wake word detection: sub-frame, effectively instant (<10ms/80ms block)
- STT (faster-whisper, int8, ~3s utterance): roughly 300-800ms
- LLM time-to-first-token (Ollama, 3B model, CPU): roughly 200-900ms
- TTS first-sentence synthesis (Piper, ONNX, CPU): roughly 60-200ms

Because TTS playback of sentence *N* overlaps with LLM generation +
synthesis of sentence *N+1* (see `SpeechStreamer`), the user-perceived
latency to "assistant starts talking" is close to
`stt + time-to-first-token + first-sentence-synthesis`, not the full
response's total generation time.

## LLM backend: local Ollama, or Groq's cloud API

Two interchangeable backends live behind one interface
(`l_assistant.llm.base.LlmClient`), selected by `llm.provider` in config.
`Orchestrator` and `ConversationMemory` never know which one is active.

**Ollama (default)** -- fully offline after the first `ollama pull`, zero
rate limit, zero per-token cost, ever. The trade-off is local compute:
response quality/speed is bounded by your own CPU/GPU.

**Groq** -- if you'd rather not run a local model at all. Cloud-hosted,
OpenAI-compatible, and fast (LPU inference). Setup:

```bash
export GROQ_API_KEY=gsk_...          # https://console.groq.com/keys
python -m l_assistant --config config/groq.yaml
```

You do not need Ollama installed at all with this profile -- skip that
step of the setup scripts entirely.

The honest trade-off, spelled out rather than glossed over: Groq's free
tier is rate-limited (roughly 30 requests/minute as of mid-2026), which
is precisely the kind of usage-limited tier the Ollama default exists to
avoid. For one person talking to their own assistant at normal
conversational pace this is very unlikely to ever bind -- you can't
naturally generate 30 turns in 60 seconds -- but it's a real limit that
didn't exist with Ollama, not a theoretical one.

Model choice matters here in a way it doesn't with a model you host
yourself: Groq deprecated `llama-3.1-8b-instant` and
`llama-3.3-70b-versatile` in 2026 (the former shut down 2026-08-16) --
`config/groq.yaml` ships with `openai/gpt-oss-20b` instead. If you pick a
different model, `verify_install.py` / startup logs will warn (not
silently fail) if it's not in Groq's current catalog.

Both backends also support tool calling (see "System control" below) --
`OllamaClient` routes tool-calling requests through Ollama's own
OpenAI-compatible endpoint (`/v1/chat/completions`) rather than its
native `/api/chat`, specifically so both backends return `tool_calls`
in the same wire format and share one parser (`l_assistant/llm/base.py`).

## STT backend: local faster-whisper, or Groq's cloud Whisper API

The same local/cloud split as the LLM, independently configurable via
`stt.provider`, because you might want a local LLM with cloud STT or vice
versa. `l_assistant.stt.factory.build_stt_engine` selects between them; both
implement the same `transcribe(pcm_bytes, sample_rate) -> str` interface.

**local (default)** -- faster-whisper, fully offline, zero rate limit.

**groq** -- offloads transcription to Groq's cloud Whisper API
(`whisper-large-v3-turbo` by default), so no local STT model ever needs
to be downloaded or run. This is genuinely useful on low-spec hardware
where even a small local Whisper model is a meaningful CPU cost per
turn -- the same motivation the original system-control blueprint this
feature was built from cited. Same trade-offs as Groq's LLM backend:
adds a network round trip and Groq's free-tier rate limit; uses the same
`GROQ_API_KEY` by default (`stt.api_key_env_var`, separately
configurable if you want a different key for STT vs. LLM).

```yaml
stt:
  provider: "groq"
  groq_model: "whisper-large-v3-turbo"
```

`config/system-control.yaml` uses this combined with a cloud LLM, so
nothing but the wake word and TTS run locally at all.

## System control (optional, off by default)

Set `tools.enabled: true` (or pass `--tools`) to let the LLM launch/close
applications, adjust volume, minimize windows or control one specific
window, control browser tabs, search Google, and create/copy/rename/
move-to-trash files -- the full capability set from the "L"
system-control blueprint this was built from, plus per-window control.
`config/system-control.yaml` is a ready-made profile with this on.
Arbitrary command execution and power management (shutdown/reboot) are
each a further, separate opt-in -- see their own subsections below;
`tools.enabled: true` does not turn either of those on by itself.

**Read this before turning it on.** The blueprint this shipped from had
two real defects that this implementation does not have, on purpose:

- **No shell string building, anywhere except `run_command`, which is
  the point of that tool.** The blueprint's `os.system(f"start
  {app_name}")` / `os.system(f"taskkill /f /im {app_name}.exe")` pattern
  is textbook command injection -- an `app_name` of `"notepad & del /f
  /q C:\"` executes as two commands under `os.system`. Every process
  launch/kill, file operation, and window/power action in
  `l_assistant/tools/system_tools.py` uses `subprocess`'s list form with
  `shell=False` (or `os.startfile` on Windows) instead, so there is no
  shell for an injected argument to reach, plus a character-level
  validation pass as defense in depth. The one deliberate exception is
  `run_command` (section below) -- running an arbitrary command *is*
  shell execution by definition, so that tool's safety boundary is
  "always confirm the exact text before it runs" instead.
- **Destructive actions ask first, out loud.** `file_operation` with
  `action="delete"` or `"rename"`, anything that would overwrite an
  existing file, `power_control` with `action="shutdown"`/`"reboot"`,
  and every `run_command` call regardless of what it contains, raise
  `ConfirmationRequired` instead of running -- `Orchestrator` speaks the
  question, and only a clear spoken "yes" on the *next* utterance
  (captured without needing the wake word again) executes it. Silence,
  "no", or anything ambiguous is treated as declined -- fails safe, not
  open. Turn this off with
  `tools.require_confirmation_for_destructive: false` if you really want
  it -- note that this one flag controls confirmation for *all* of
  delete/rename/power/run_command, not just file operations -- but the
  default is on for a reason: a single STT mis-transcription or
  LLM-hallucinated path talking directly to a destructive action with no
  human in the loop is a bad trade for a voice interface, and is a
  documented failure mode of this exact pipeline, not a hypothetical one
  -- see `l_assistant/stt/confidence.py` and the 2026-08-20 note in
  `config/system-control.yaml`.

**Also on by default:** every file operation (and `run_command`'s
`working_directory`, if given) is restricted to `tools.allowed_roots`
(your home directory if left empty) -- checked by resolving the target
path (following symlinks) and confirming it lands inside an allowed
root, so neither a wildly wrong path nor a deliberate traversal attempt
(`../../something`) ever reaches `shutil`/`Path`. `file_operation`'s
`delete` action moves to the OS trash/recycle bin (`send2trash`) rather
than permanently deleting, specifically so a misheard or LLM-misjudged
delete is recoverable independently of whatever you've set
`require_confirmation_for_destructive` to.

**What's genuinely unguarded:** `open_application` will launch anything
on your `PATH` the LLM asks for, and `adjust_volume`/
`minimize_all_windows`/`control_window`/`manage_browser_tabs` have no
confirmation gate (they're reversible/low-stakes by nature -- changing
which window is on top or how loud things are isn't in the same risk
class as deleting a file or running a command). `tools.max_tool_iterations`
(default 4) bounds how many tool-call round trips one turn can make, so
a model that keeps calling tools can't hang a turn forever.

Tool calling itself is necessarily non-streaming (see `l_assistant/llm/base.py`
and `orchestrator.py`'s docstrings) -- a tool-invoking turn doesn't get
the "speak while still generating" latency win a plain conversational
turn does, though the final answer still plays through the same
overlapped sentence-chunked TTS once it's ready.

**Linux needs two extra system packages** for `manage_browser_tabs`
(new/close/next/previous tab), `minimize_all_windows`, and
`control_window`:

```bash
sudo apt-get install xdotool wmctrl
```

`setup_linux.sh` offers to install these (optional, only if you plan to
use `tools.enabled`); `verify_install.py` warns (not fails) if they're
missing when a loaded config has tools enabled. Windows needs nothing
extra for these -- they use `ctypes`/native Windows APIs instead
(written against the documented Win32 API surface; not verified against
a real Windows session -- see the focus-stealing caveat in
`control_window`'s Windows branch specifically).

The twelve tools on by default with `tools.enabled: true`:
`open_application`, `close_application`, `adjust_volume`,
`minimize_all_windows`, `control_window`, `open_website`, `search_web`
(site-aware -- see below), `manage_browser_tabs`, `file_operation`
(now including a read-only `list` action -- see below),
`remember_user_fact`, `forget_user_fact` (see "Durable user profile"
above), `play_youtube_search` (see below).

**`file_operation`'s `list` action** is the only read-only one, and
exists specifically so an ambiguous reference ("delete the folder on my
desktop", no name given) gets resolved against the real directory
contents instead of either a guessed name or an unnecessary clarifying
question -- the system prompt instructs the model to `list` first and
only ask if that still leaves more than one plausible match. It carries
no confirmation requirement of its own (nothing destructive happens by
listing), and doesn't change what happens next: a `delete`/`rename` of
whatever `list` found still goes through the exact same spoken
confirmation as if the user had named the path directly.

**`search_web`** takes an explicit `site` parameter (`"google"` default,
`"youtube"`, or any other name for a `site:`-scoped Google search)
specifically so "search X on YouTube" opens an actual YouTube search
instead of a Google search for "X YouTube" -- the tool schema instructs
the model to always set `site` rather than folding it into the query
text, with a worked example in the schema description itself.

**`control_window`** acts on one specific window (by a case-insensitive
substring of its title, or the currently-focused window if no target is
given) rather than every window at once -- "maximize Brave", "switch to
VS Code", "minimize this". If more than one open window matches the
target, the first one found is used; there's no voice-friendly way to
disambiguate further without a lot of extra machinery, so this optimizes
for the common case over handling the rare one gracefully.

**`play_youtube_search`** is the exact-video counterpart to
`search_web(site="youtube")`: "play the first video on YouTube after
searching for X" needs an actual video playing, not a results page the
user still has to click through. Resolves the real video URL via
`yt-dlp` (`ytsearch1:` -- a real, actively-maintained resolver, not
hand-rolled parsing of YouTube's search-results HTML, which breaks
every time YouTube changes its page markup) and opens that URL
directly. Worth knowing plainly: this is not the official YouTube Data
API -- no API key, no daily quota, but also not an officially
sanctioned integration path the way the Data API is. If that tradeoff
matters for your use case, the Data API is the alternative, at the cost
of a Google Cloud project and its quota limits.

### Power management (`tools.power_management_enabled`, off by default)

Adds `power_control` (`shutdown` / `reboot` / `cancel`). Off by default
even with `tools.enabled: true` -- it's a different risk class from
everything above (unsaved work lost, not just a moved file), so it gets
its own flag rather than riding along with the others. `shutdown`/
`reboot` always require spoken confirmation (see above) *and* a minimum
delay (`tools.power_min_delay_seconds`, default 15s, enforced regardless
of what's requested) before anything actually happens, specifically so
there's always a real window to say "cancel" even if the confirmation
itself was given by mistake. `cancel` needs no confirmation and works
whether or not anything is currently scheduled.

Windows needs nothing extra (`shutdown.exe` is available to a normal
interactive user by default, no elevation required, unless Group Policy
restricts it in your environment). **Linux needs a one-time setup step**
first, since shutting down requires root and this project deliberately
does not run the whole voice-assistant process as root for the sake of
one feature:

```bash
sudo ./scripts/setup_linux_power_control.sh
```

This grants your user passwordless `sudo` for exactly `shutdown -h`,
`shutdown -r`, and `shutdown -c` -- nothing else -- via a validated
`/etc/sudoers.d/l-power-control` drop-in. Read the script before
running it; it prints the exact rule and asks for confirmation before
writing anything. Remove it any time with
`sudo rm /etc/sudoers.d/l-power-control`. Without this step,
`power_control` on Linux fails with a clear error pointing back here
rather than hanging on a password prompt the process can't answer.

### Command execution (`tools.command_execution_enabled`, off by default)

Adds `run_command` -- runs an arbitrary shell command (PowerShell on
Windows, bash on Linux) as a fallback for anything none of the other
tools cover. Off by default even with `tools.enabled: true`, for the
same reason as power management: it's a qualitatively different
capability from the rest of this file. Unlike file operations (where
`delete`/`rename` need confirmation but `copy`/`create` don't, because
destructiveness is classifiable), **every** `run_command` call requires
confirmation, with no way to mark specific commands as pre-approved --
there's no reliable way to pattern-match an arbitrary command string as
"safe" by inspection (a denylist of dangerous-looking substrings is
exactly the kind of thing that's both bypassable and full of false
positives on legitimate commands), so instead of trying, the model
always has to relay the literal command back to you first. If there's a
specific command you want to run with zero friction, the right way to
get that is a small dedicated tool method for it (following the pattern
of everything else in `system_tools.py`), not an allowlist bolted onto
`run_command`.

`working_directory`, if given, is checked against `tools.allowed_roots`
like `file_operation`'s paths are -- but that's a convenience/sanity
constraint on where the command *starts*, not a sandbox on what it can
*do*; true sandboxing of arbitrary command execution (containers,
AppArmor, Windows Job Objects) is out of scope here. The actual safety
mechanism for this tool is the mandatory spoken confirmation, not path
scoping.

## Continuous conversation / follow-up mode (`conversation.follow_up_enabled`, off by default)

Without this, every command needs its own "hey jarvis" -- ask a
question, get an answer, and you're back to square one for the next
thing. With it on, L keeps listening for up to
`conversation.follow_up_window_seconds` (default 8s) after it finishes
answering, so "hey jarvis, what's the weather" / "and set a timer for 10
minutes" / "cancel that" can be one back-and-forth instead of three
separate wake-ups. Say an explicit close ("that's all", "goodbye",
"stop listening", ...) to end the window early, or just don't say
anything and it reverts to normal wake-word-gated listening on its own
once the window elapses. `conversation.max_consecutive_follow_ups`
(default 6) is a hard cap on how many turns can chain without hearing
the wake word again, independent of the timeout -- it bounds a
sustained false-positive run (a TV or podcast left on in the room, say)
to a fixed number of turns instead of an unbounded one.

**Read this before turning it on.** Every other listen in this codebase
happens because the wake word gated it first -- `wake_word.threshold`
has to clear before STT ever runs at all. A follow-up listen has no
wake-word check in front of it. It leans entirely on
`stt.no_speech_prob_threshold`/`avg_logprob_reject_threshold`
(`l_assistant/stt/confidence.py`) to reject ambient-noise hallucinations
instead -- a real, tested defense for the local (faster-whisper) STT
backend, but *not confirmed* for this profile's Groq cloud backend (see
the STT section above and that module's docstring). Watch for the
one-time `GroqCloudSTT: verbose_json segment schema` log line after your
first few utterances to find out which case you're actually in before
leaving this on for long unattended stretches. If it turns out Groq
isn't returning the confidence fields, either keep the follow-up window
short, or switch `stt.provider` to `local` specifically to get that
protection back with certainty.

Implementation-wise, this reuses the exact same `skip_wake_word_once`
mechanism the confirmation flow already used (see `Orchestrator.run`'s
docstring) rather than adding a second, parallel way to skip the wake
word -- a follow-up listen and a confirmation-answer listen are both
just "the next utterance doesn't need the wake word," they only differ
in which timeout and which post-transcript handling apply.

## Running in the background (system tray)

Set `tray.enabled: true` (or pass `--tray`) to run L as a background
process with a tray icon instead of a foreground terminal app:

```bash
python -m l_assistant --tray
```

- A single-instance lock (`tendo.singleton`) refuses to start a second
  copy -- launching it twice logs a clear error and exits instead of
  running two assistants against the same microphone.
- The tray menu's Pause/Resume genuinely pauses and resumes wake-word
  listening (via a thread-safe `asyncio.Event`, see
  `Orchestrator.request_pause`/`request_resume`) -- unlike the source
  blueprint's version of this menu item, which was a non-functional
  `lambda: print("Toggled")` stub.
- **"Start with Windows"** is a real, checkable toggle in the tray menu
  (Windows only -- see "Autostart" below), backed by `l_assistant/
  autostart.py`. Checked state is read live from the registry every
  time the menu opens, not cached, so it stays accurate even if changed
  some other way (a fresh install, manual `regedit`) between opens.
- If the pipeline fails to start (e.g. Ollama unreachable, or a missing
  `GROQ_API_KEY`), the tray icon shows a native notification with the
  error and closes, rather than sitting there looking "running" over a
  dead pipeline.
- Exiting via the tray menu ends the process; a voice turn in progress at
  that exact moment ends with it (the pipeline runs on a daemon thread --
  see `l_assistant/tray/app_tray.py`'s docstring for why a full graceful
  cross-thread cancellation wasn't worth the complexity for an Exit
  action specifically).
- `pystray`/`tendo`/`pillow` are imported lazily, only when tray mode
  actually runs -- `python -m l_assistant` in the default foreground mode never
  touches them, and doesn't need a display server to work.
- The tray/taskbar icon (`l_assistant/tray/icon_art.py`) is drawn code,
  not an imported image file -- a bold geometric "L" plus a small
  "listening" accent dot, tuned for legibility down to 16x16 (most
  fonts blur into mud at that size; two rectangles don't). Uses
  `tray.icon_color` for the accent, so it reflects your configured
  color live. `packaging/assets/app.ico` (the static icon baked into
  the built exe/installer) is generated from the exact same drawing
  code -- see `scripts/generate_icon.py` -- so the two never drift
  apart into looking like different apps.

### Autostart (Windows only)

Toggle "Start with Windows" in the tray menu, or from the command line:

```bash
python -m l_assistant --enable-autostart
python -m l_assistant --disable-autostart
```

Adds/removes one string value under
`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` --
the standard per-user autostart mechanism. Deliberately **not** a
Windows service: a service runs under a system account with no access
to the interactive desktop session's microphone/speakers, which would
make a voice assistant that can't actually hear or speak. HKCU means no
admin elevation prompt and no machine-wide state -- it's scoped to
whichever Windows user account enabled it, consistent with everything
else in this file being per-user (see "Data locations"). The launch
command always includes `--tray` regardless of what `tray.enabled`
happens to be set to in whatever config is active when autostart runs,
since running invisibly in the background is the entire point of
autostart.

## Durable user profile (name, preferences)

Distinct from conversation memory (above): a small JSON file
(`user_profile.json` in the data directory -- see "Data locations")
holding facts that persist indefinitely, independent of the rolling
conversation window's compaction cycle. Say "remember that I prefer
Celsius" or "my name is Alif" and it's still known next month, whether
or not the conversation that mentioned it has long since scrolled out
of context and been summarized away.

Two tools, always available alongside the rest of `tools.enabled`'s
core set (no separate opt-in flag -- storing a fact in a local file the
user could open and read themselves isn't in the same risk class as
`run_command` or `power_control`):

- **`remember_user_fact(key, value)`** -- the model calls this whenever
  the user states something worth keeping.
- **`forget_user_fact(key)`** -- "forget my name" removes it.

Known facts are injected into the system prompt on every turn (see
`Orchestrator._run_turn`), not held behind a "recall" tool the model
has to think to call -- a real assistant doesn't need to be asked to
remember your name once you've told it once.

**Isolated per machine, by construction, not by anything in this
feature specifically**: the profile lives under `platformdirs`'
per-user data directory (see "Data locations"), which resolves to a
different path for every distinct Windows user account. Copy
`l_assistant.exe` to another machine, or have a different user run it
on the same machine, and it starts with an empty profile -- there's
nothing to configure to get this, it falls out of where the file lives.

## Setup

### Linux (developed and tested on Linux Mint; any current Debian/Ubuntu
family distro works the same way)

```bash
git clone <this repo>
cd l-voice-assistant
chmod +x scripts/setup_linux.sh
./scripts/setup_linux.sh
```

That script: installs `libportaudio2` (the one system package this
project needs outside of pip -- PortAudio's Linux wheel doesn't bundle
it, unlike the Windows wheel), creates a venv, installs Python
dependencies (including the `openwakeword` `--no-deps` step explained in
`requirements.txt` -- its PyPI release has a broken Linux-only
`tflite-runtime` dependency this project doesn't need), pre-downloads the
wake word / STT / TTS models, and installs + pulls a model for Ollama.

**Not using Ollama?** If you're going with `llm.provider: "groq"`
instead (see "LLM backend" above), skip step 5/5 of the script entirely
-- Ollama isn't needed at all in that case, just a `GROQ_API_KEY`.

Non-apt distros (Fedora, Arch, etc.): install the equivalent of
`python3-venv`, `python3-pip`, and `portaudio`/`libportaudio2` through
your package manager, then run steps 2 onward from
`scripts/setup_linux.sh` manually.

### Windows 11

Open PowerShell:

```powershell
git clone <this repo>
cd l-voice-assistant
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\setup_windows.ps1
```

There is no Windows-only system package step: `sounddevice`'s Windows
wheel bundles the PortAudio DLL automatically. If Ollama isn't already
installed, the script tells you to grab the installer from
<https://ollama.com/download/windows> and re-run.

### Both platforms: verify, then run

```bash
python scripts/verify_install.py   # checks imports, models, mic, Ollama
python -m l_assistant
```

First run downloads whatever `scripts/download_models.py` didn't already
cache (openWakeWord's shared models, the Whisper model, the Piper voice).
Subsequent runs start cold in a few seconds.

## Running

```bash
python -m l_assistant                        # normal wake-word-gated mode
python -m l_assistant --no-wake-word         # open-mic mode: every utterance is a turn
python -m l_assistant --list-devices         # print audio device indices/names
python -m l_assistant --input-device 2       # pin a specific mic
python -m l_assistant --config config/quiet-room.yaml
python -m l_assistant --log-level DEBUG
python -m l_assistant --tools                # enable OS control (see "System control" above)
python -m l_assistant --tray                 # run in the system tray instead of the foreground
```

Say the wake word ("Hey Jarvis" by default), wait for the spoken
acknowledgment (`wake_word.ack_phrase`, "Yes?" by default), then speak.
A second or so of silence ends your turn. With
`conversation.follow_up_enabled: true` (off by default -- see
"Continuous conversation" above), you can keep talking after that
without repeating the wake word, for a configurable window.

## Configuration

Everything tunable lives in `config/default.yaml`, validated by the
Pydantic schema in `src/l_assistant/config.py` (see that file for the exact
defaults and constraints, e.g. `vad.aggressiveness` must be 0-3). Only
override what you're changing -- unset keys fall back to defaults.
High-value settings also accept environment variable overrides
(`L_LLM_MODEL`, `L_LLM_BASE_URL`, `L_STT_MODEL_SIZE`,
`L_TTS_VOICE`, `L_WAKE_WORD_MODEL`, `L_LOG_LEVEL`, `L_DATA_DIR`).

Common changes:

- **Different wake word**: `wake_word.model: "alexa"` or `"hey_mycroft"`
  (built-in), or an absolute path to a custom-trained `.onnx` model.
- **Faster/more accurate STT**: `stt.model_size: "tiny.en"` (fastest) up
  to `"large-v3"` (most accurate, slowest, needs more RAM).
- **Different voice**: `tts.voice: "en_US-amy-medium"` etc. -- browse
  <https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md>,
  then re-run `python scripts/download_models.py`.
- **Multi-turn conversation without repeating the wake word**:
  `conversation.follow_up_enabled: true` -- see "Continuous conversation"
  above before turning this on.
- **Different LLM**: `llm.model: "qwen2.5:3b-instruct"` (or any model
  you've `ollama pull`ed) -- bigger models are slower but smarter; this
  project has no built-in ceiling on model size, only your hardware does.
- **Don't want a local LLM at all**: `llm.provider: "groq"` -- see the
  "LLM backend" section above and `config/groq.yaml`.
- **Don't want local STT either**: `stt.provider: "groq"` -- see "STT
  backend" above.
- **OS control from voice commands**: `tools.enabled: true` -- read
  "System control" above first, it's off by default for real reasons.
- **Run in the background**: `tray.enabled: true`, or `--tray`.
- **GPU acceleration**: set `tts.use_cuda: true` (requires
  `onnxruntime-gpu`); Ollama uses your GPU automatically if one is
  present and supported, no config needed on the L side.

## Data locations

Resolved via `platformdirs` (see `src/l_assistant/platform_utils.py`), so this
is the same code path on both OSes even though the actual paths differ:

- Linux: `~/.local/share/l-assistant/`
- Windows: `%LOCALAPPDATA%\l-assistant-project\l-assistant\`

Contains: `models/wakeword/`, `models/voices/`, `l_memory.db`,
`logs/l_assistant.log`. Override the whole tree with `data_dir:` in config or
`L_DATA_DIR`.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the sentence chunker, config schema/validation, conversation
memory (rolling window + compaction), VAD endpointing (including the
silence-vs-timeout distinction), the system-control tools engine (path
sandboxing, confirmation-required flows, command-injection resistance),
OpenAI-format tool-call response parsing, and the tool-calling
orchestration loop (bounded iteration, confirmation flow, message
threading) via fakes -- everything that doesn't require a microphone,
speaker, or downloaded model, so they run identically in CI on either OS.
77 tests as of this writing.

## Troubleshooting

Run `python scripts/verify_install.py` first -- it checks every import,
both audio directions, the wake word models, the configured TTS voice,
and Ollama reachability in one pass, and tells you exactly which one
failed.

- **`ModuleNotFoundError: tflite_runtime` or a `pip install` failure
  mentioning `tflite-runtime`**: you (or a tool) ran `pip install
  openwakeword` directly instead of following `requirements.txt` /
  the setup scripts. openwakeword's PyPI release has a broken
  unconditional `tflite-runtime` dependency on Linux that this project
  doesn't need (ONNX backend only). Fix: `pip uninstall openwakeword`,
  then `pip install --no-deps "openwakeword>=0.6.0"` (its real
  dependencies are already covered by `requirements.txt`).
- **`OSError` / no sound on Linux at startup**: `libportaudio2` isn't
  installed -- `sudo apt-get install libportaudio2` (already handled by
  `setup_linux.sh`, but relevant if you installed dependencies manually).
- **Wake word never triggers**: lower `wake_word.threshold` (e.g. `0.4`);
  confirm the right mic is selected with `--list-devices` /
  `--input-device`; check `--log-level DEBUG` output for prediction
  scores.
- **Wake word triggers on background noise/TV**: raise
  `wake_word.threshold` and/or `wake_word.vad_threshold` (the latter
  gates detections on openWakeWord's own built-in Silero VAD check).
- **Assistant cuts you off mid-sentence**: raise `vad.silence_timeout_ms`.
- **Assistant waits too long after you finish talking**: lower
  `vad.silence_timeout_ms`.
- **Every turn takes ~20 seconds before anything happens, and/or you see
  `Utterance ran the full 20s without a natural pause` in the log**:
  background noise (fan, HVAC, room echo) is being intermittently
  misclassified as speech, which keeps resetting the silence timer so it
  never reaches `silence_timeout_ms` -- every recording runs to
  `vad.max_utterance_seconds` (12s in `system-control.yaml`, down from
  the 20s default -- see the 2026-08-22 note in that file) instead of
  stopping naturally after you finish talking. `vad.aggressiveness`
  already defaults to `3` (WebRTC VAD's strictest setting -- there's no
  stricter one to raise it to) and `speech_frame_vote_threshold` is
  already raised once for exactly this reason. If it's still happening,
  the noise is likely loud enough, relative to speech, to pass spectral
  classification regardless of how strict those two get -- at that
  point `vad.min_rms` (an amplitude floor checked BEFORE spectral
  classification, off by default) is the more targeted fix; run `python
  scripts/vad_calibrate.py` to measure a real number for your room
  instead of guessing one.
- **L transcribes its own voice as your command** -- e.g. the
  transcript in the log is literally the wake acknowledgment phrase, or
  a garbled version of L's own last response, instead of anything you
  said: the mic picking up L's own TTS output through the speakers
  (there's no acoustic echo cancellation -- see `orchestrator.py`'s
  docstring for the three-layer defense that exists instead). This
  should now be rare: the mic is muted at the source for the entire
  duration of every spoken utterance, not just drained afterward. If
  you're still seeing it: check `[WARN]`/`Discarding likely self-echo`
  log lines first -- layer 3 may already be catching and discarding it
  for you, in which case nothing is actually wrong. If it's genuinely
  getting through, raise `vad.post_speech_settle_ms` above its 250ms
  default, lower your speaker volume, or increase mic-to-speaker
  distance.
- **`OllamaUnavailableError` at startup**: Ollama isn't running --
  `ollama serve` on Linux, or launch the Ollama app on Windows.
- **`OllamaModelMissingError` at startup**: `ollama pull <model-name>`
  the model named in `llm.model`.
- **`GroqUnavailableError: ... is not set` at startup**: `export
  GROQ_API_KEY=gsk_...` (get one free at
  <https://console.groq.com/keys>) before running with
  `--config config/groq.yaml` / `llm.provider: groq`.
- **`GroqUnavailableError: ... 401 Unauthorized`**: the key is wrong,
  expired, or revoked -- regenerate it at the link above.
- **`GroqUnavailableError: ... rate limit hit`**: you've exceeded Groq's
  free-tier request rate (see the "LLM backend" section above). Either
  wait out the `retry-after` window it reports, or switch back to
  `llm.provider: "ollama"`.
- **`GroqSttUnavailableError`**: same fixes as the `GroqUnavailableError`
  entries above, but for `stt.api_key_env_var` / `stt.provider: "groq"`.
- **`FileNotFoundError` mentioning `wake_word.model` at startup**: you're
  pointing at a custom `.onnx` wake word file (e.g. for "Hey, L") that
  hasn't been trained yet -- see "System control" above and
  `config/system-control.yaml`'s comments. Use a built-in name
  (`hey_jarvis`, `alexa`, `hey_mycroft`) until you've trained one.
- **Tray mode (`--tray` / `tray.enabled: true`): no icon appears, and
  nothing else visible happens either** (packaged `L.exe` builds
  especially -- `l_agent.spec` builds a `console=False` app, so there's
  no terminal for a traceback to even print to): check
  `logs/l_assistant.log` first -- `cli_main` now catches any unhandled
  startup error, logs it there with a full traceback, and (on Windows)
  pops a message box, specifically so this can't fail completely
  silently. If you're on an older build without that fix, or the log
  doesn't explain it, run from a terminal instead
  (`python -m l_assistant --tray`, not the packaged exe) to see the
  raw exception directly.
- **A tool call is refused with `'... is outside the allowed
  directory'`**: working as designed -- add the path to
  `tools.allowed_roots` in config if it should be permitted.
- **A destructive action seems to hang waiting for confirmation**: it's
  listening for a plain "yes"/"no" on your next utterance, without the
  wake word -- just answer it. It gives up after
  `tools.confirmation_timeout_seconds` (12s default) of silence and
  cancels automatically.
- **`AlreadyRunningError` when starting `--tray`**: another L instance
  already holds the single-instance lock -- check your system tray, or
  Task Manager / `ps aux | grep l_assistant` for a stray process.
- **Tray mode fails with an `ImportError` about pystray/tendo/pillow**:
  these ship in `requirements.txt` already, so this only happens with a
  manual/partial install -- `pip install pystray tendo pillow`.
- **First turn is slow, later turns are fast**: expected -- STT/TTS/LLM
  all pay a one-time warmup cost on first inference;
  `WhisperSTT.warmup()` / `PiperTTS.warmup()` in `main.py` already
  absorb most of this before you ever say the wake word, but the very
  first Ollama call still pays for model load into memory. (Not
  applicable if you're using `stt.provider: "groq"` -- there's no local
  model to warm up.)

## Building a standalone Windows executable

`packaging/` has everything from the original blueprint's packaging
requirements, adapted to this project's actual dependency tree:

- `l_agent.spec` -- PyInstaller spec. Uses `collect_all()` for
  `onnxruntime`, `ctranslate2`, `faster_whisper`, `piper`, and
  `openwakeword` -- packages with compiled extensions and/or bundled data
  files (piper's espeak-ng-data, onnxruntime's shared libraries) that
  PyInstaller's default import scanning misses on its own. **This spec
  was actually built and its output inspected** as part of producing this
  project (on Linux, since no Windows machine was available here -- see
  the spec file's own header comment for exactly what was verified: all
  376 espeak-ng-data files, the espeak bridge extension, onnxruntime's
  and ctranslate2's compiled binaries were confirmed present in the
  packed output). That's real evidence the dependency-collection approach
  works, from a Linux build; `build_exe.yml` (below) is what validates
  the actual Windows target.
- `pyinstaller_hooks/hook-webrtcvad.py` -- overrides a bundled
  PyInstaller community hook that crashes the build outright
  (`ImportErrorWhenRunningHook`) because it assumes the `webrtcvad`
  package name, not `webrtcvad-wheels` (which this project uses
  specifically for Windows wheel availability -- see requirements.txt).
  This isn't a hypothetical -- it's what actually happened running this
  spec for the first time, and the fix that resulted from it.
- `installer.iss` -- Inno Setup script: Start Menu + optional desktop
  shortcut, an opt-in "start automatically when Windows starts" task
  (writes a `--tray` launch command to the registry Run key under the
  exact same value name -- `"L Voice Assistant"` -- `l_assistant/
  autostart.py` and the tray menu's toggle use; only if that task is
  checked during install), bundles the three example config profiles
  alongside the exe.
- `assets/app.ico` -- generated from `l_assistant/tray/icon_art.py` (the
  same drawing code the live tray icon uses -- see "Running in the
  background" above) via `python scripts/generate_icon.py`, not a
  hand-made or placeholder file. Regenerate it after changing
  `icon_art.py`, or to bake in a different accent color than the
  default, before running `pyinstaller`.

Build locally (Windows, inside the venv from `setup_windows.ps1`):

```powershell
pip install pyinstaller
python scripts\generate_icon.py   # only needed if icon_art.py changed since the last build
cd packaging
pyinstaller l_agent.spec
# dist\L.exe now exists; optionally also build the installer:
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

Or let `.github/workflows/build_exe.yml` do it: pushes to `main` build
both `L.exe` and `L_Setup.exe` on GitHub's `windows-latest` runner
and upload them as workflow artifacts -- this is the one part of
packaging that runs on real Windows, not a Linux approximation of it.

The exe does not bundle model weights (a local Whisper model, the Piper
voice, openWakeWord's pretrained models) -- those download once into the
user's data directory on first launch, same as the normal pip install.
Baking them in would add several hundred MB to a GB+ for one-time
convenience; `config/system-control.yaml`'s cloud STT + cloud LLM combo
(matching the original blueprint's low-spec-hardware motivation) means
that profile doesn't even need a local Whisper model downloaded at all.

## Licensing notes

This project's own code has no license file bundled here -- that's a
choice for you to make for your fork/deployment. What you should know
before redistributing a *build* of it to other people:

- **Piper TTS is GPL-3.0** as of the actively-maintained
  OHF-Voice/piper1-gpl fork this project depends on (the original
  MIT-licensed `rhasspy/piper` was archived read-only in October 2025).
  Bundling/shipping Piper inside a closed-source distribution triggers
  GPL-3.0's reciprocal obligations. Running it locally for your own use,
  as this project does, has no such implication.
- **`pystray` (tray mode only) is LGPLv3**, not MIT as its ubiquity might
  suggest -- worth knowing before a closed-source redistribution the same
  way Piper is, though LGPL's obligations are narrower (they attach to
  modifications of pystray itself, not merely to linking against it).
- Everything else in the dependency table above is MIT, BSD,
  Apache-2.0, or PSF-licensed.

## Known limitations / extension points

Documented rather than hidden, per this project's own "no silent gaps"
standard:

- **No barge-in / full-duplex conversation.** Can't be talked over
  mid-response -- the mic is muted for the entire duration of every
  spoken utterance (see Architecture and `orchestrator.py`'s module
  docstring for why: no dependency-light, prebuilt-wheel AEC library
  exists for this project's cross-platform bar). `AudioPlayer` already
  exposes `stop_immediately()` for whoever wires one in.
- **No custom "Hey, L" wake word model out of the box.** A wake word for
  an arbitrary phrase needs real audio sample collection plus a training
  run through openWakeWord's own pipeline -- not something producible
  without that data. `config/system-control.yaml` ships with the
  built-in `hey_jarvis` model instead so it works today; see that file's
  comments and `wake/wake_word.py` for the (now much clearer, since a
  recent fix) error you'd get pointing at an untrained model path, and
  the training pipeline link.
- **Confirmation flow is single-pending.** Only one destructive action
  can be awaiting confirmation at a time -- if the model somehow queued
  two in one turn, only the first is asked about (the tool loop returns
  as soon as the first `ConfirmationRequired` fires, so this is more of
  a design invariant than an edge case that could actually occur given
  how `_run_tool_loop` is structured, but worth stating explicitly).
- **Tray Exit doesn't gracefully cancel an in-flight turn.** See
  `l_assistant/tray/app_tray.py`'s docstring -- the pipeline thread is a daemon
  thread, torn down with the process on Exit rather than cleanly
  cancelled first.
- **Single active session at a time.** `ConversationMemory` is keyed by
  `session_id` and the schema supports multiple concurrent sessions in
  the same SQLite file, but the orchestrator itself only ever runs one.
- **English defaults.** `faster-whisper`, Piper, and openWakeWord are all
  multilingual; swapping `stt.language`, `tts.voice`, and training/using
  a different wake word model is enough to change languages, but the
  default `config/default.yaml` and `hey_jarvis`/`.en` model choices are
  English-first out of the box.
- **`manage_browser_tabs`'s "next"/"previous" don't work on macOS.**
  AppleScript's `keystroke` command types literal characters, which
  works for "new"/"close" (Cmd+T/Cmd+W) but would type the literal word
  "tab" into whatever has focus instead of pressing the Tab key for
  cycling between tabs. Fails with a clear error rather than doing that;
  not fixed because macOS isn't a target platform here (Linux Mint +
  Windows 11 -- see Setup). Everything else in `tools/system_tools.py`
  from before 2026-08-20 (apps, volume, files, search, minimize) works
  identically on Windows and Linux, both live-verified, not just written
  to spec.
- **`control_window`, `power_control`, `run_command`'s Windows branches
  are written against the documented Win32/PowerShell surface, not
  live-verified** -- no Windows machine was available while adding
  these. The Linux side has a mix: `run_command` and the
  `setup_linux_power_control.sh` sudoers rule were both actually
  executed (real subprocess calls, real `visudo`/`sudo -l` checks
  confirming the generated rule authorizes exactly the invocations
  `power_control` produces and nothing else); `control_window`'s Linux
  branch is covered by tests that mock `subprocess.run` with realistic
  `wmctrl -l` output rather than against a live desktop session, since
  this environment doesn't have one. Exercise both platforms' new tools
  for real before relying on them, same as you would for anything else
  in this list.
- **`no_speech_prob`/`avg_logprob`-based hallucination rejection
  (`l_assistant/stt/confidence.py`) is confirmed for the local faster-whisper
  backend, not confirmed for Groq's cloud API.** `groq_cloud_whisper.py`
  requests `verbose_json` and reads the fields defensively if present,
  but whether Groq's `/audio/transcriptions` endpoint actually returns
  per-segment confidence data the way OpenAI's own Whisper API does
  wasn't verified against a live account -- it logs once, at runtime, to
  tell you which case you're actually in (see that module's docstring).
  If it turns out absent, `wake_word.threshold` and
  `vad.speech_frame_vote_threshold` are doing the real work for that
  backend instead.
- **`run_command`'s `working_directory` scoping is a sanity check, not a
  sandbox.** It confirms the command *starts* inside `allowed_roots`;
  what the command itself goes on to do is unconstrained by anything
  except the mandatory spoken confirmation. True sandboxing (containers,
  AppArmor, Job Objects) is out of scope here.

## Project layout

```
l-voice-assistant/
├── config/
│   ├── default.yaml
│   ├── groq.yaml                 # cloud LLM profile
│   └── system-control.yaml       # "L" persona: OS control + cloud LLM/STT
├── requirements.txt
├── pyproject.toml
├── scripts/
│   ├── setup_linux.sh
│   ├── setup_linux_power_control.sh  # sudoers rule for tools.power_management_enabled
│   ├── setup_windows.ps1
│   ├── download_models.py
│   ├── generate_icon.py          # regenerates packaging/assets/app.ico from icon_art.py
│   └── verify_install.py
├── packaging/
│   ├── l_agent.spec           # PyInstaller spec (verified, see above)
│   ├── pyinstaller_hooks/hook-webrtcvad.py
│   ├── installer.iss             # Inno Setup script
│   └── assets/app.ico
├── .github/workflows/build_exe.yml
├── src/l_assistant/
│   ├── main.py                   # entry point, wires everything together
│   ├── config.py                 # Pydantic schema + YAML/env loader
│   ├── platform_utils.py         # the one place OS path differences live
│   ├── autostart.py              # Windows Registry Run-key autostart
│   ├── logging_setup.py
│   ├── audio/
│   │   ├── capture.py            # MicrophoneStream (sounddevice -> asyncio)
│   │   ├── playback.py           # AudioPlayer + synthesized earcon
│   │   └── vad.py                # FrameVad + EndpointDetector (webrtcvad)
│   ├── wake/wake_word.py         # WakeWordDetector (openWakeWord, ONNX)
│   ├── stt/
│   │   ├── whisper_engine.py     # WhisperSTT (local, faster-whisper)
│   │   ├── groq_cloud_whisper.py # GroqCloudSTT (cloud)
│   │   ├── confidence.py         # shared hallucination-rejection policy (both backends)
│   │   └── factory.py            # build_stt_engine() -- picks local or Groq
│   ├── tts/
│   │   ├── piper_engine.py       # PiperTTS
│   │   └── streamer.py           # SpeechStreamer (overlapped synth+playback)
│   ├── llm/
│   │   ├── base.py               # LlmClient interface + tool-call data shapes/parser
│   │   ├── factory.py            # build_llm_client() -- picks Ollama or Groq
│   │   ├── ollama_client.py      # OllamaClient (default: local, no rate limit)
│   │   └── groq_client.py        # GroqClient (optional: cloud, rate-limited)
│   ├── tools/
│   │   ├── system_tools.py       # SystemToolsEngine -- app/volume/file/window/power/command/profile/YouTube control
│   │   └── registry.py           # OpenAI-format schemas (core + gated) + ToolDispatcher
│   ├── tray/
│   │   ├── app_tray.py           # system tray, single-instance lock, autostart toggle
│   │   └── icon_art.py           # shared icon-drawing code (live tray icon + static app.ico)
│   ├── memory/
│   │   ├── conversation_memory.py # SQLite rolling window + compaction
│   │   └── user_profile.py        # durable JSON user-facts store
│   ├── pipeline/
│   │   ├── state.py              # AgentState enum + PendingConfirmation
│   │   └── orchestrator.py       # the FSM tying everything together
│   └── utils/text.py             # SentenceChunker
└── tests/                        # hardware-free unit tests (223 as of this writing)
```
