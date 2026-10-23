# Whisper Meeting

Whisper Meeting is an independent Kiro Crew App Kit app for durable, local
meeting transcription on Apple Silicon Macs. Its primary promise is that audio
which has been acknowledged as durable is either transcribed or remains visibly
recoverable; it is never silently discarded because recognition, an agent, or a
connection fell behind.

This project is not a fork, plugin layer, or replacement UI for Kiro Crew's
built-in Meetings app. It owns its capture, storage, recognition, recovery, and
meeting lifecycle, and it does not share the built-in Meetings or Chat Dictation
streaming STT path.

## Why this project exists

Short dictation and multi-hour meetings have different correctness requirements.
Dictation optimizes for low-latency partial text. A meeting recorder must survive
slow inference, pauses, page reloads, backend restarts, and an End operation with
audio still waiting to be recognized.

Whisper Meeting therefore treats durable PCM audio, rather than a live
connection or partial transcript, as the source of truth:

```text
microphone -> AudioWorklet -> IndexedDB -> durable HTTP ACK -> PCM spool
                                                        -> local Whisper
                                                        -> final transcript
                                                        -> optional agents
```

Recognition and agents are downstream consumers. Neither is allowed to stop or
discard capture merely because it is slow.

## MVP status

Version **0.3.6** is a usable, field-tested MVP on the current Apple Silicon
target. The implemented path includes:

- durable 500 ms PCM capture with an IndexedDB upload queue and idempotent
  backend acknowledgement;
- local Metal-accelerated `large-v3-turbo` recognition through
  `pywhispercpp`, with no CPU fallback;
- independent VAD/pause segmentation, automatic language detection, and
  best-effort English, Simplified Chinese, and mixed-language transcription;
- Pause/Resume epochs and End draining without dropping the captured tail;
- retained source PCM, visible recognition coverage, and segment-level retry;
- meeting history and transcript export as Markdown, text, and JSON;
- a fixed Note Taker, a fixed Sketch Artist, and Meeting Chat context;
- low-frequency, coalesced agent work rather than one provider task per
  transcript segment;
- Markdown notes, sandboxed Mermaid/HTML sketches, and self-contained sketch
  export that does not require network access to render.

The core recording, Pause/Resume, agent output, and export paths have been used
in live meetings. The repository also contains restart, retention, batching,
sanitization, and deterministic recovery tests. Broader qualification across
older Apple Silicon models and a single long-running field scenario combining
route changes, a page reload, a gateway restart, and a multi-hour thermal run
remain outside the current evidence.

## Supported MVP boundary

| Area | Current support |
|---|---|
| Host | macOS on Apple Silicon; current measured target is an M4 Pro Mac |
| Surface | Kiro Crew desktop app; one local owner |
| Meetings | One active recording per app installation |
| Input | One browser-selected microphone; a virtual device such as BlackHole works as that microphone |
| Recognition | One pinned `large-v3-turbo` GGML model, Metal required |
| Languages | English, Simplified Chinese, and mixed Chinese/English, best effort |
| Transcript | Committed final segments; no live partial-caption stream |
| Recovery | Durable browser queue, backend PCM spool, restart reconciliation, and per-segment re-recognition |
| Raw audio | Kept for 7 days and the latest 20 ended meetings, under a 5 GiB total cap |
| Agents | Fixed Note Taker and Sketch Artist through the configured Kiro Crew ACP backend |
| Meeting Chat | Broadcast context and corrections to both agents; it does not rewrite the source transcript |
| Export | Transcript Markdown/text/JSON, notes Markdown, standalone sketch HTML, and combined Markdown |

### Not supported in this MVP

- Intel Macs, Windows, Linux, mobile, or a remote-browser deployment;
- CPU Whisper fallback, alternate transcription providers, selectable local
  models, cloud STT, translation, or live interpretation;
- native system-audio capture or multiple simultaneous inputs; use a virtual
  audio device when system audio is required;
- speaker diarization, speaker naming, or `Speaker 1/2/3` assignment;
- arbitrary user-defined meeting agents or a persistent one-to-one chat with an
  individual agent;
- automatic action-item extraction/review, task filing, calendar integration,
  attachments, or meeting presets;
- a reusable speech-correction dictionary or automatic edits to the immutable
  transcript;
- indefinite audio retention or a built-in audio playback/editor experience.

## How it differs from Kiro Crew Meetings

Use the built-in **Meetings** app when you want a broader meeting-assistant
workflow. Use **Whisper Meeting** when durable local capture and recoverability
are the priority and the deliberately narrow platform boundary fits.

| | Whisper Meeting | Built-in Meetings |
|---|---|---|
| Product boundary | Independently installed third-party App Kit app | Ships with Kiro Crew |
| STT architecture | App-owned durable PCM queue and local Metal Whisper worker | Kiro Crew shared streaming STT endpoint |
| Coupling to Chat Dictation | None | Shares Kiro Crew's STT subsystem |
| Transcript contract | Final committed text derived from retained audio | Live streaming transcript lines |
| Slow recognition | Capture continues and PCM accumulates for later recognition | Governed by the shared streaming session |
| Pause and End | Drain captured audio through durable upload and recognition | Flush the live session and agent queues |
| Raw-audio recovery | Bounded PCM retention plus segment retry | Transcript/session workflow rather than this app's PCM ledger |
| Meeting agents | Fixed Note Taker and Sketch Artist, dispatched at low frequency | Note Taker, Sketch Artist, and Task Extractor, normally batched about every 30 seconds |
| Typed input | One Meeting Chat broadcast to both agents | Broadcast plus direct messaging to an individual agent |
| Workflow features | Transcript, notes, sketch, and export | Action-item review/filing, calendar providers, attachments, translation, dictionary, and audio import |
| Platform target | Apple Silicon macOS desktop only | Built-in app declares macOS, Linux, and Windows support |
| Release lifecycle | Versioned and upgraded independently | Upgraded with Kiro Crew |

The comparison describes different product goals, not a compatibility promise.
The two apps use different data directories and formats, and meetings cannot be
migrated between them.

## Privacy and ACP agents

Audio capture and Whisper inference stay on the Mac. The Note Taker and Sketch
Artist receive finalized transcript text and Meeting Chat context through the
user's configured Kiro Crew ACP backend. If that backend uses a cloud model,
including typical Claude, Kiro, or Codex configurations, the text crosses that
provider boundary. Audio is never included in the agent request, and Whisper
Meeting adds no cloud endpoint of its own.

The app currently inherits Kiro Crew's global `agent.acp_backend`; it does not
choose a backend per meeting or per agent. The agent contract is backend-neutral
and is designed to work with Kiro CLI ACP and Codex ACP as well as Claude ACP.
The live MVP validation to date used Claude. Kiro CLI and Codex should be treated
as compatible but not yet end-to-end qualified by this project; Codex also
requires the `codex-acp` adapter to be installed and available to Kiro Crew.

## Requirements

- Kiro Crew 0.10.0 or newer
- macOS on Apple Silicon
- Kiro Crew desktop app
- Third-party app execution enabled for this app in **Settings → Security**
- A Metal-enabled `pywhispercpp` runtime

The App Kit installer provisions `pywhispercpp>=1.5,<2` and a local OpenCC
converter from `requirements.txt`. Startup checks for both `pywhispercpp` and its native
`_pywhispercpp` extension and reports actionable setup errors through model
status. Recognition children receive the same app-owned dependency directory
instead of relying on packages bundled with Kiro Crew. Model self-test
separately refuses CPU-only builds; there is no CPU fallback.

Successful transcription keeps source PCM for local audit and segment-level
re-recognition. The default policy keeps seven days and the latest 20 ended
meetings, subject to a 5 GiB hard cap; deleting a meeting deletes its audio
immediately.

## Install

Build the UI before installing from a source checkout:

```bash
npm ci --prefix ui
npm run build --prefix ui
kirocrew app install .
kirocrew app enable whisper-meeting
```

Alternatively, create a deterministic source archive:

```bash
python scripts/package_local_meeting_app.py \
  --output dist/whisper-meeting-0.3.6.tar.gz
```

Extract the archive and pass its root directory to `kirocrew app install`.
App-owned `data/` is preserved across Kiro Crew and app updates.

## Verify

```bash
PYTHONPATH=. python -m pytest -q test
npm test --prefix ui -- --run
npm run typecheck --prefix ui
npm run build --prefix ui
```

The design, acceptance criteria, benchmark records, and remaining wall-clock
field gates are in
[`docs/rfc-local-meeting-transcription-app.md`](docs/rfc-local-meeting-transcription-app.md).
The meeting-agent extension and its qualification evidence are in
[`docs/mvp-meeting-agents-plan.md`](docs/mvp-meeting-agents-plan.md).
