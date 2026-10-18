---
title: Local Meeting Transcription App — durable, local-first capture on macOS
status: draft
author: KiroCrew contributors
created: 2026-10-17
last-audited: 2026-10-17
audited-at: 56825903dc
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Local Meeting Transcription App

> **Status:** `implementing`. This document defines a first MVP for the independently
> installed **Whisper Meeting** App Kit app (`whisper-meeting`). It does not modify
> or replace the built-in Meetings app. Phases 0 through 5 are implemented in
> this standalone repository; Phase 5 wall-clock field qualification remains. The App Kit platform
> already provides installable renderer-plus-backend apps, an HTTP reverse proxy
> with SSE support, signed backend requests, minimum Kiro Crew version gates, and
> a `data/` directory preserved across app updates.

## Summary

Build a new local meeting transcription app as an independent App Kit package.
The MVP runs only on Apple Silicon Macs, captures microphone audio in the app UI,
persists every audio chunk before upload, transcribes with one bundled local
Whisper worker (`whisper.cpp` through `pywhispercpp`), and makes loss visible and
recoverable.

The app does not import code or state from the existing Meetings app or Chat
Dictation. It does not call Kiro Crew's `/api/ws/stt` path and does not need a
WebSocket proxy. Its UI talks to its own App Kit backend through ordinary HTTP;
server-to-UI progress uses SSE through the existing App Kit reverse proxy.

The published identity is `whisper-meeting`, the display name is **Whisper
Meeting**, and this repository root is the installable source package.

The primary product promise is stronger than "live captions":

> Once the UI reports an audio range as durably acknowledged, that range will
> either appear in the transcript or remain visibly recoverable. It will never
> disappear silently.

The app has its own release cycle. A Kiro Crew upgrade does not automatically
upgrade the app, but the installed app remains present. The app declares
`minKiroCrewVersion`, and App Kit preserves its `data/` directory when the app is
updated.

## Motivation

The current Meetings experience shares the streaming STT subsystem with Chat
Dictation. That subsystem was designed around short interactive dictation and
live partial text. Long meetings have different requirements:

- recording lasts hours rather than seconds or minutes;
- page reloads, backend restarts and slow inference must be ordinary recovery
  cases;
- pause and end must wait for captured audio to become durable and transcribed;
- correctness depends on an auditable audio sequence, not only on the latest
  partial transcript;
- a storage failure must stop capture visibly instead of discarding old audio,
  while recognition lag must not stop capture.

Fixes inside the shared STT path can reduce individual races, but they retain the
coupling between two products with different durability contracts. This proposal
therefore starts from a separate application boundary rather than another
Meetings patch.

## Goals

- Record one microphone on one Apple Silicon Mac for at least two hours.
- Transcribe locally without sending audio or transcript text to a cloud service.
- Support Chinese, English and mixed Chinese/English speech on a best-effort
  basis, using independent pause/VAD segmentation to refresh language detection
  at natural utterance boundaries.
- Persist audio before network delivery and acknowledge it only after durable
  backend storage.
- Resume upload after a tab reload, app restart or backend restart.
- Make capture, upload, recognition and drain progress independently visible.
- Give Pause and End exact, testable drain semantics.
- Store meeting history and export Markdown, plain text and JSON.
- Install, update and run as an independent App Kit app.
- Keep the design small enough for one backend process and one recognition
  worker in the MVP.

## Non-goals

- Modifying, embedding into, or replacing the current Meetings app.
- Sharing code, hooks, session state or recovery logic with Chat Dictation.
- Migrating existing Meetings recordings or transcripts.
- Compatibility between the new app's records and the current Meetings format.
- Native system-audio capture or mixing, and multiple simultaneous input
  devices. A user-selected virtual audio device such as BlackHole is treated as
  the single microphone input and needs no special app support.
- Speaker diarization, speaker naming, translation or live interpretation.
  Best-effort `Speaker 1`/`Speaker 2` labels are deferred to a post-MVP,
  post-meeting processing phase and do not shape the MVP completion contract.
- Automatic summaries, action items, calendar integration or agent workflows.
- Live provisional captions. The MVP shows committed final transcript only.
- Cloud transcription, AWS Transcribe, Apple Speech, or selectable providers.
- Multiple local model families or a general model marketplace.
- Mobile, Windows, Linux or remote-browser support.
- Indefinite raw-audio archiving or audio playback. Source PCM is retained for a
  bounded recovery window and storage budget.
- WebSocket transport or a new WebSocket proxy in Kiro Crew.

## Target platform

The MVP supports this deliberately narrow matrix:

| Dimension | MVP support |
|---|---|
| Host | Initial measured target: Mac mini with Apple M4 Pro and 64 GB RAM |
| Browser surface | Kiro Crew desktop app page |
| User | One local owner |
| Input | One microphone selected through browser media capture |
| Meetings | One active recording per app installation |
| Recognition | Metal-enabled `whisper.cpp` through `pywhispercpp`, fixed to `large-v3-turbo` |
| Languages | Best-effort Chinese, English and mixed-language recognition |
| Network | Loopback App Kit traffic only after installation and model setup |
| Storage | App-owned SQLite database and bounded audio spool under `data/` |

The Phase 0 development gate is intentionally valid only for the measured M4 Pro
host on macOS 27.0.1. Widening the public support floor to older Apple Silicon is
a later release qualification task, not an inferred promise from this benchmark.
The model, runtime and accelerator are fixed: the MVP uses
`large-v3-turbo` through Kiro Crew's local Whisper route (`whisper.cpp` through
`pywhispercpp`) with the Metal backend linked and active. A CPU-only build is
unsupported and must fail preflight rather than degrade silently. Phase 0 does
not compare MLX, Qwen ASR or a cloud provider. Intel Macs are not a fallback
target.

## User experience

### First run

1. The user installs the app from a local source or registry.
2. Because third-party executable apps are denied by default, the user grants
   this app permission in **Settings → Security → Third-party apps**. The broader
   `agent.apps_allow_third_party=true` switch also admits it but is not required;
   a per-app grant is preferred.
3. The user enables the app.
4. The app checks host architecture, free disk space and microphone permission.
5. The app verifies that `pywhispercpp` reports an active Metal backend. A
   CPU-only build fails setup with installation guidance; it is never accepted as
   a slower fallback.
6. The app locates an existing verified Whisper GGML artifact when compatible,
   otherwise downloads the one supported artifact into its own `data/models/`,
   verifies its hash, and performs a short inference self-test.
7. The Record button remains disabled until the backend check and self-test both
   succeed.

### Recording

1. The user creates a meeting with an optional title.
2. Pressing Record starts a new capture epoch and the microphone indicator. The
   capture engine is a module-level singleton owned by the app bundle, not by the
   route component, so navigation away from and back to the app page does not
   stop the microphone.
3. The transcript appears only as committed final segments. The MVP performs no
   partial decode and displays no provisional captions.
4. The status panel continuously shows:
   - microphone capture state;
   - local chunks waiting for upload;
   - the last durably acknowledged sequence;
   - backend audio pending recognition;
   - recognizer state;
   - the audio position committed to final transcript;
   - spool usage and any backpressure condition.
5. Pause stops new capture and waits for all captured audio to become durable and
   transcribed before the meeting becomes paused.
6. Resume starts a new capture epoch. Sequence numbers do not reset within an
   epoch and transcript ordering uses audio position, not wall-clock completion.
7. End follows the same drain contract as Pause, then seals the meeting.

### History

The history page lists completed, paused, recovering and failed meetings. A
meeting can be opened, exported or deleted. A meeting with recoverable audio is
never labelled complete.

## Reliability contract

### 1. Audio is the source of truth

Transcript text is a derived projection. The durable input is a sequence of
audio chunks identified by meeting, capture epoch and monotonically increasing
sequence number.

Each chunk carries at least:

```json
{
  "meeting_id": "uuid",
  "capture_epoch": 1,
  "sequence": 42,
  "captured_at": "RFC3339 timestamp",
  "sample_start": 336000,
  "sample_count": 8000,
  "sample_rate": 16000,
  "channels": 1,
  "encoding": "pcm_s16le",
  "sha256": "hex digest"
}
```

The binary PCM body accompanies this metadata. The initial target is a 500 ms
chunk, but correctness must not depend on that duration.

### 2. Browser-side durable queue

The capture pipeline is:

```text
microphone -> AudioWorklet -> IndexedDB -> HTTP uploader -> durable ACK
```

The UI writes a complete chunk into IndexedDB before making it eligible for
upload. It deletes the local copy only after the backend acknowledges the exact
meeting, epoch, sequence and hash as durable.

A reload reopens the IndexedDB queue and resumes upload before allowing a new
capture epoch. Closing the page with unacknowledged chunks must produce a visible
warning using the browser lifecycle affordances available to the host.

The AudioWorklet, IndexedDB writer and uploader are owned by a module-level
capture singleton. Page components subscribe to its state but do not create or
destroy it. An app route unmount therefore cannot close the microphone. A full
window reload or Kiro Crew process exit ends the live capture epoch; IndexedDB
preserves every chunk committed before that interruption and recovery exposes a
visible capture boundary.

### 3. Backend durable ACK

The backend pipeline is:

```text
HTTP chunk
  -> validate identity, sequence and hash
  -> atomically write the PCM spool object
  -> commit chunk metadata in SQLite
  -> return ACK
  -> enqueue recognition work
  -> commit transcript segment
  -> retire covered spool data
```

An ACK means the backend can recover the chunk after process restart. It does not
mean the chunk has already been transcribed.

Receiving the same sequence and hash again is an idempotent success. Receiving
the same meeting, epoch and sequence with different bytes is an integrity error
that stops the meeting until the user intervenes.

### 4. Recognition lag never stops capture

Recognition backlog and storage exhaustion are different conditions. Slow or
stalled recognition does not stop capture: acknowledged PCM remains in the spool
and is transcribed later, including after the meeting ends. At 16 kHz, mono,
16-bit PCM, two hours of source audio is about 230 MB before filesystem and
database overhead, so ordinary multi-hour backlog is a disk-management problem,
not a reason to interrupt the meeting.

IndexedDB and the backend spool are bounded only by explicit storage policy: a
minimum free-disk reserve, browser quota and an operator-visible retention cap.
The MVP reserves 5 GiB of free disk, caps the browser queue at 512 MiB and caps
the backend spool at 2 GiB. At the measured 16 kHz mono PCM cost, those caps hold
about 4.66 and 18.64 hours of raw audio respectively, before browser/database
overhead. Capture stops with a blocking error only when the next chunk cannot be
durably stored without crossing one of those storage limits or when the storage
write itself fails. The app never makes room by deleting untranscribed or
unacknowledged audio.

### 5. Drain semantics

Pause and End are operations, not instantaneous flags. The state machine is:

```text
recording
  -> capture_stopped
  -> uploading
  -> transcribing
  -> drained
  -> paused | ended
```

`drained` means all audio samples captured before the operation's cut point are:

- durably acknowledged by the backend;
- present in the backend's chunk ledger without a sequence gap; and
- covered by committed final transcript segments or an explicit, visible
  recognition error retaining the source audio.

The UI may be closed during a drain. Reopening the meeting resumes the operation
and shows its current phase.

## Architecture

```text
App Kit page
  AudioWorklet
  IndexedDB chunk queue
  HTTP chunk uploader
  SSE status client
        |
        | /apps/<app-name>/api/*
        | standard App Kit HTTP reverse proxy
        v
App backend process
  request authentication
  session service
  SQLite WAL database
  bounded PCM spool
  one recognition scheduler
  one local Whisper worker
  append-only event ledger
```

The app is a renderer-plus-backend App Kit app. It ships no Electron main-process
code and adds no Kiro Crew core route. The backend uses an automatically assigned
loopback port and is started and stopped by the App Kit lifecycle.

The UI uses HTTP request/response for commands and chunk upload. It uses one SSE
stream for state changes and progress. App Kit permits an `event-stream`
response through the reverse proxy only while it emits an event or `:` comment
at least once every 60 seconds. The app therefore emits an SSE heartbeat comment
every 30 seconds, including while state is unchanged. No bidirectional stream is
required.

## Backend API

All routes are below `/apps/<app-name>/api/`. Names below are the app backend's
view after proxy prefix handling.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/health` | Cheap unsigned loopback health probe only |
| `GET` | `/model` | Model presence, hash, load and self-test state |
| `POST` | `/model/install` | Install the single supported model artifact |
| `GET` | `/meetings` | List meetings and recovery state |
| `POST` | `/meetings` | Create a meeting |
| `GET` | `/meetings/{id}` | Read meeting metadata and progress |
| `DELETE` | `/meetings/{id}` | Delete a non-active meeting and its data |
| `POST` | `/meetings/{id}/start` | Start a new capture epoch |
| `PUT` | `/meetings/{id}/audio/{epoch}/{sequence}` | Idempotently store one chunk |
| `POST` | `/meetings/{id}/pause` | Stop capture at a cut point and begin drain |
| `POST` | `/meetings/{id}/resume` | Start the next capture epoch after pause |
| `POST` | `/meetings/{id}/end` | Stop capture, drain and seal the meeting |
| `GET` | `/meetings/{id}/events` | SSE progress and transcript events |
| `GET` | `/meetings/{id}/transcript` | Current final transcript projection |
| `GET` | `/meetings/{id}/export` | Export Markdown, text or JSON |

Pause and End return an operation id immediately. Their progress and terminal
state arrive through SSE and remain queryable from the meeting resource.

Every route except `/health` verifies the App Kit reverse-proxy HMAC. The
manifest opts into `signedPrincipal`; mutable and transcript-bearing routes also
require the signed principal to identify the owner session.

## Data model

SQLite runs in WAL mode. At minimum the database contains these logical tables:

- `meetings`: identity, title, lifecycle state and timestamps;
- `capture_epochs`: start, stop and cut-point sample positions;
- `audio_chunks`: epoch, sequence, hash, sample range, spool path and state;
- `transcript_segments`: stable id, source sample range, language, coverage kind
  (`speech`, `silence` or `empty_decode`), text and commit time;
- `filtered_transcript_segments`: rejected text, confidence evidence and gate
  reason for local audit;
- `operations`: Pause and End drain state;
- `events`: monotonically increasing SSE replay cursor;
- `model_state`: artifact identity, hash and self-test result.

Transcript segments are ordered by `(capture_epoch, audio_start_sample, id)`, not
by inference completion time. A final segment records the source sample range it
covers so drain completeness can be calculated rather than guessed.

The app owns this layout under its App Kit `data/` directory:

```text
data/
  meetings.sqlite3
  models/
  sessions/<meeting-id>/spool/
  exports/
```

Spool writes use a temporary file, `fsync`, and atomic rename before the database
transaction marks a chunk durable. Recovery reconciles the database and spool in
both directions before accepting new work.

## Recognition pipeline

The MVP has one recognition worker and one fixed model profile. This avoids the
model-selection, resource arbitration and compatibility surface of a general
local inference service.

Recognition uses an app-owned pause/VAD segmenter. It may follow the measured
adaptive-RMS approach, but it does not import Kiro Crew's Dictation VAD, session
or STT lifecycle. Durable 500 ms capture chunks remain the source of truth; VAD
boundaries only group those samples for recognition and cannot acknowledge,
delete or reorder audio. Each completed speech segment performs a fresh
automatic language decision before `large-v3-turbo` inference. A maximum
utterance duration cuts speech that contains no pause, and adjacent recognition
windows overlap so the transcript assembler can avoid clipping boundary words.

Mixed Chinese/English recognition is explicitly best effort. The Phase 0 probe
shows that pause-aligned segmentation preserves both languages, while one
uninterrupted mixed-language window may still favour only one. The MVP does not
claim perfect language-switch detection or word-for-word accuracy.

The worker:

1. claims the next contiguous audio range;
2. constructs a bounded recognition window with overlap;
3. invokes the local runtime outside the request path;
4. uses energy VAD only to find likely pause boundaries; a low-confidence
   window still reaches Whisper after 10 seconds, or as one bounded tail when
   Pause or End drains it;
5. applies the runtime no-speech/log-probability gate and the app's known-phrase
   gate, preserving rejected text in the filtered side record;
6. converts Chinese output to Simplified Chinese and records the detected
   language;
7. normalizes timestamps to absolute meeting sample positions;
8. atomically commits final segments and the covered-through position;
9. emits transcript and progress events.

If the worker crashes, its claim expires on backend restart and the durable audio
is retried. Recognition errors retain their audio and are visible in the meeting
state. The request-serving process must remain responsive while inference runs.

## Failure and recovery behaviour

| Failure | Required result |
|---|---|
| Upload request times out | UI retries the same sequence and hash |
| Duplicate upload arrives | Backend returns the original durable ACK |
| Tab reloads | IndexedDB queue resumes before new capture starts |
| Backend restarts | SQLite and spool reconciliation resumes recognition |
| Recognizer crashes | Source audio remains durable and work is retried |
| SSE disconnects | UI reconnects from its last event cursor, then refreshes state |
| App or Kiro Crew restarts while paused | Meeting remains paused and recoverable |
| App or Kiro Crew restarts while draining | Drain resumes from durable state |
| Recognition falls behind | Capture continues; acknowledged PCM accumulates for later recognition |
| Browser quota, free-disk reserve or retention cap is reached | Capture stops visibly before the next unsafe write; no old audio is deleted |
| Sequence gap is observed | Meeting shows the missing range and cannot seal |
| Model is missing or invalid | Recording cannot start |
| `pywhispercpp` has no active Metal backend | Setup fails; recording cannot start and there is no CPU fallback |
| App update stops the backend | Browser queue retains unacknowledged chunks and upload resumes after restart |

## Privacy and security

- Audio and transcript processing stays local after the model artifact is
  installed.
- The app declares no agents, skills, MCP servers, crons or cloud connections in
  the MVP.
- The backend binds only to loopback and trusts neither loopback location nor an
  app name as authentication; it verifies the gateway's proxy signature.
- The unsigned `/health` route returns only readiness and performs no file or
  model work.
- Mutable routes require the signed owner principal and reject `agent-tool`,
  `app-token` and unauthenticated principals.
- Transcript exports are explicit user actions. The app exposes no MCP resource,
  skill or dedicated agent tool for transcript or audio in the MVP.
- Raw spool files remain after successful recognition. They are removed only
  when the user deletes the meeting or the bounded retention policy runs. The
  default keeps seven days and the latest 20 meetings, with a 5 GiB hard cap;
  the oldest ended meetings are retired first when the cap wins.
- The install manifest declares the minimum Kiro Crew version and the smallest
  permission set required by the final implementation.

The MVP explicitly accepts that app-owned `data/` is not isolated from an agent
that already has a file-reading tool and access to the Kiro Crew data home. App
manifests cannot declare a new sandbox-hidden path, and
[`rfc-app-sandbox-isolation.md`](rfc-app-sandbox-isolation.md) does not provide
third-party app file isolation. The spool and transcript are therefore local and
owner-scoped through the HTTP API, but not confidential from a locally trusted
agent. This is disclosed in setup and privacy copy. Encryption and agent-proof
storage are outside the MVP.

## Updates and compatibility

The implementation is not added under `src/kiro_crew/apps/builtins/`. It is
packaged as an independently installable App Kit app, preferably in its own
repository and release pipeline.

- The app manifest pins `minKiroCrewVersion` to the first platform version whose
  HTTP/SSE proxy and signed-principal contracts it relies on.
- Kiro Crew upgrades and app upgrades are independent.
- An app upgrade preserves `data/` and runs schema migration before starting the
  backend.
- Database migrations are forward-only, transactional and versioned.
- A build that cannot read the stored schema refuses to start rather than
  rewriting or discarding it.
- The MVP makes no hot-update promise during an active recording. App Kit parses
  `setup.onUpdate` but does not execute it, so the app has no update-veto hook on
  which to base such a promise. The UI tells the user to End before updating.
  If an update nevertheless stops the backend, the open page keeps new chunks in
  IndexedDB and retries after restart. If the host page itself restarts, recovery
  closes the interrupted capture epoch and exposes the interruption; it never
  represents the meeting as continuously captured.
- No compatibility promise exists with the built-in Meetings app's database,
  transcript format or live session.

## MVP acceptance criteria

The MVP is releasable only when all of these pass on every supported host class:

1. **Two-hour run:** a two-hour recording finishes with a gap-free chunk ledger
   and every captured sample either covered by transcript or retained with an
   explicit error.
2. **Network faults:** deterministic injected timeouts, connection resets, 5xx
   replies and duplicate deliveries produce no missing or duplicate audio range.
3. **Reload recovery:** reloading the app with browser-side queued chunks resumes
   upload and does not start a conflicting epoch.
4. **Backend recovery:** terminating the backend during upload and recognition
   preserves acknowledged audio and resumes work after restart.
5. **Recognizer recovery:** terminating the model worker preserves the spool and
   retries the claimed range once.
6. **Pause correctness:** Pause does not report paused until its cut point is
   durably uploaded and transcribed.
7. **End correctness:** End does not report complete while a sequence gap,
   recoverable chunk or recognition error remains.
8. **Backpressure:** an arbitrarily delayed recognizer does not stop capture;
   forcing browser quota, the free-disk reserve or the retention cap stops
   capture with a blocking message and never evicts the oldest data.
9. **Update survival:** app update and Kiro Crew restart preserve completed,
   paused and recoverable meetings; an interrupted active meeting reopens with a
   visible capture boundary and any queued chunks resume upload.
10. **Privacy:** packet capture during recording and recognition shows no audio
    or transcript leaving loopback.
11. **Observability:** every injected failure is visible in the UI and durable
    event log; no failure is represented as an indefinitely spinning normal
    state.
12. **Resource bound:** the Phase 0 target host can record and transcribe for two
    hours without unbounded memory, database, spool or event-log growth.
13. **Metal required:** preflight proves the loaded `pywhispercpp` binary exposes
    and uses the whisper.cpp Metal backend; a CPU-only build is refused.

Tests use signals and injected faults rather than wall-clock sleeps. Long-run
tests may use generated PCM at accelerated upload speed, while one real-time
field test validates browser capture and power/thermal behaviour.

## Delivery plan

Each phase is independently reviewable and can be abandoned without modifying
the current Meetings or Dictation products.

### Phase 0 — feasibility and measured choices

Build throwaway probes for Kiro Crew's existing local Whisper runtime
(`whisper.cpp` through `pywhispercpp`) compiled with Metal, and its GGML model
path. Preflight records the backend registry reported by the loaded native binary
and fails the phase if Metal is absent or inactive; a stock CPU-only wheel is not
an acceptable result. Measure the candidate GGML sizes, with `large-v3-turbo`
included, on the minimum Apple Silicon target. For every model, report its
real-time factor and resulting backlog after a continuous one-hour speech input.
Also measure chunk size, thermal behaviour, disk cost and the App Kit SSE path.
MLX, Qwen ASR, Apple Speech and cloud providers are outside this phase.

**Exit criteria:** one durable benchmark record proves the tested native binary
has Metal active; reports real-time factor and one-hour accumulated backlog for
every candidate GGML model; selects the model; and shows the selected model
sustains the required workload on the initial measured target host. It also sets the
free-disk reserve and retention cap, verifies a 30-second SSE heartbeat survives
the App Kit proxy, and proves capture continues while recognition is deliberately
stalled. A CPU-only result cannot pass. Implementation phases are blocked until
this passes.

Run `scripts/benchmark_local_meeting_whisper.py` with the Python interpreter
bundled in `KiroCrew.app`. The probe verifies the SHA-256 of both candidate
weights, rejects a run unless the native loader says it selected the Metal
backend, generates reproducible Mandarin and English speech locally with macOS
voices, and records separate Chinese, English and within-window mixed-language
results. Its default workload is one hour of 30-second windows for each of
`small` and `large-v3-turbo`; it reports measured real-time factor and the
recognition backlog reconstructed from the window arrival rate.

Phase 0 uses this acceptance checklist:

- [x] The [durable JSON record](evidence/local-meeting-whisper-phase0-2026-10-17.json)
  identifies the host, bundled Python and loaded
  native extension without recording a hardware serial number or private audio.
- [x] Both model files match their catalog SHA-256 before loading.
- [x] Both model loads contain native `using MTL… backend` evidence; the linked
  backend registry alone is not accepted as proof of active Metal use.
- [x] Both models complete one hour of alternating Chinese, English and mixed
  30-second windows, with real-time factor and final/maximum backlog recorded.
- [x] Raw Han-character CER (with script form disclosed), English WER and the
  mixed-window transcript select `large-v3-turbo`: it has the stronger Mandarin
  result, zero WER on the synthetic English fixture, zero one-hour backlog and
  ample headroom for per-segment language detection.
- [x] A [sustained-load run](evidence/local-meeting-whisper-thermal-phase0-2026-10-17.json)
  continuously decodes one hour of audio per model with no idle gaps, records
  nominal macOS thermal state at every quartile, and shows neither model degrades
  anywhere near real time.
- [x] The [storage probe](evidence/local-meeting-storage-phase0-2026-10-17.json)
  measures 113.51 MiB of allocated storage per audio hour, including its SQLite
  ledger; the MVP uses a 5 GiB free-disk reserve, 512 MiB browser queue cap and
  2 GiB backend spool cap.
- [x] The [SSE probe](evidence/local-meeting-sse-phase0-2026-10-17.json) observes
  three 30-second comment heartbeats through the production App Kit proxy
  handler, with the stream surviving beyond its 60-second idle boundary.
- [x] With recognition deliberately stalled, the probe durably stores and
  verifies all 14,400 chunks from two hours of capture without deleting one.

The [mixed-boundary probe](evidence/local-meeting-mixed-boundary-phase0-2026-10-17.json)
shows that neither candidate preserves both languages when an entire 30-second
mixed window receives one automatic language decision. Both preserve Chinese
and English when the known switch boundary is decoded as two speech segments.
This establishes that segmentation can solve the measured failure. The MVP will
implement and validate its own pause/VAD segmenter and treats switches inside one
uninterrupted utterance as best effort.

### Phase 1 — independent app shell and durable store

Create the external App Kit package, manifest, backend lifecycle, authenticated
routes, SQLite schema and meeting history without microphone capture.

**Exit criteria:** install, enable, restart, update and schema migration preserve
synthetic meeting records; no resource is registered outside the app.

Phase 1 completed on 2026-10-17:

- [x] `app.json` validates as an external macOS/arm64 App
  Kit package with an app-owned page and backend only.
- [x] Every non-health meeting route verifies the App Kit proxy HMAC and a
  replay-protected `owner-session` principal claim.
- [x] SQLite WAL schema v4 contains the MVP tables, refuses an unknown future
  schema and migrates a v1 fixture without losing its meeting record.
- [x] Ten backend/store tests pass, including restart persistence, owner
  isolation, real signed HTTP requests and principal replay rejection.
- [x] The UI type-checks, builds to committed `ui/dist/index.mjs`, and its npm
  dependency audit reports no known vulnerabilities.
- [x] A clean isolated home installs and enables the app; a real Gateway starts
  its backend, and an authenticated dashboard session creates and lists a
  synthetic meeting through `/apps/whisper-meeting/api/meetings`.
- [x] Gateway stop/restart changes the automatically assigned backend port while
  preserving the record. App update and the v1-to-v4 migration also preserve it.

### Phase 2 — capture, IndexedDB and durable ACK

Add AudioWorklet capture, browser queueing, idempotent chunk upload, spool writes
and ledger reconciliation. Recognition may be a deterministic fake.

**Exit criteria:** network, reload, duplicate, backend-crash and sequence-conflict
tests pass without missing or double-counted samples.

Phase 2 completed on 2026-10-17:

- [x] The AudioWorklet resamples 16, 44.1 and 48 kHz input to independent
  16 kHz mono `pcm_s16le` chunks, emits complete 500 ms chunks and flushes the
  final partial chunk.
- [x] The module-level capture engine survives route-component unmount, stores
  each complete chunk in IndexedDB before upload, and resumes the existing
  queue before starting a new capture epoch.
- [x] The 512 MiB browser cap and 2 GiB backend spool cap reject the next unsafe
  write without evicting an earlier chunk; recognition lag does not participate
  in either capture stop decision.
- [x] The uploader retries the same epoch, sequence and hash after a network
  failure, and removes browser PCM only after an exact durable ACK.
- [x] Backend spool publication uses temporary-write, file `fsync`, atomic
  rename and directory `fsync` before the SQLite ledger commits. Startup
  reconciliation adopts a published orphan and reports a missing object.
- [x] Exact duplicate upload returns the original durable ACK while a different
  payload for the same sequence returns a conflict.
- [x] Fifteen backend/store tests and ten UI tests cover restart/reload,
  duplicate delivery, connection failure, ACK mismatch, sequence conflict,
  crash-window reconciliation, storage bounds and multi-rate chunking.
- [x] A clean isolated Gateway run proves the production App Kit path: capture
  start returns 201, the first 16,000-byte PCM upload returns 201, its exact
  retry returns 200 with `duplicate=true`, and SQLite plus the spool contain one
  8,000-sample durable range. The separately served worklet resource returns
  200 through `/apps/whisper-meeting/ui/`.

### Phase 3 — local recognition worker

Add the fixed model installer, verification, self-test and out-of-process
recognition worker with sample-positioned final segments.

**Exit criteria:** worker crash recovery, contiguous ordering, bilingual fixtures
and bounded-resource tests pass on the target host.

Phase 3 completed on 2026-10-17:

- [x] The installer accepts only the pinned 1,624,555,275-byte
  `large-v3-turbo` artifact and its catalog SHA-256. It prefers a verified local
  Kiro Crew source, copies it into app-owned `data/models/`, verifies the copy,
  atomically publishes it, and can download the same pinned artifact when no
  local source exists.
- [x] The model becomes ready only after an isolated self-test loads it and the
  native log proves `using MTL0 backend`. Recording returns
  `409 model_not_ready` until that succeeds. The app rehashes the owned artifact
  immediately before starting a native worker and refuses same-size mutation.
- [x] Recognition runs in one resident child process rather than the HTTP
  server. The scheduler uses an app-owned energy/pause segmenter, makes a fresh
  automatic language decision for each bounded window, and caps uninterrupted
  speech at 30 seconds.
- [x] SQLite schema v6 records recognition claims. A worker failure releases its
  claim without deleting PCM; the scheduler recreates the worker with bounded
  backoff until the durable queue drains, and startup returns any claim left by
  a dead process to the durable queue. Recognition children receive the
  app-owned provisioned dependency directory explicitly.
- [x] Final transcript rows are committed atomically in capture-epoch and
  absolute-sample order. Explicit empty coverage rows preserve a contiguous
  source-audio projection across pauses without displaying invented text.
- [x] Twenty-four backend tests cover app-owned model independence, hash
  refusal, bounded VAD, bilingual commit, worker failure, restart recovery and
  contiguous transcript coverage. The worker protocol keeps input windows
  bounded and the request-serving process never loads model weights.
- [x] The [Phase 3 validation record](evidence/local-meeting-phase3-2026-10-17.json)
  shows the app-owned model passing its real Metal self-test, a resident worker
  transcribing the Mandarin and English fixtures, and the complete durable
  chunk-to-VAD-to-worker-to-transcript pipeline producing two ordered jobs on
  the measured M4 Pro host.

### Phase 4 — drain state machine and UI

Add Pause, Resume, End, SSE replay, progress diagnostics, history and recovery
screens.

**Exit criteria:** every state transition is recoverable across page and backend
restart, and the Pause/End acceptance criteria pass.

Phase 4 completed on 2026-10-17:

- [x] Pause and End first flush the AudioWorklet, await the IndexedDB write
  chain, await the shared uploader and exact durable ACKs, then submit an exact
  epoch, sequence and sample cut point. The UI remains nonterminal while the
  backend is uploading or transcribing.
- [x] The backend rejects a cut with a missing or unexpected chunk, stops the
  capture epoch atomically with the operation, rejects new post-cut audio and
  keeps exact command and chunk retries idempotent.
- [x] A drain completes only when every source chunk through its cut sequence is
  transcript-covered. Pause reaches `paused`; End reaches `ended`; Resume opens
  the next epoch. End from an already drained pause does not open a throwaway
  epoch.
- [x] Pause and End force a short or unpaused recognition tail instead of
  waiting forever for the normal VAD threshold. Restart returns a dead worker's
  claim to durable work and resumes the stored drain operation.
- [x] The signed SSE route replays monotonically numbered events from either a
  query cursor or `Last-Event-ID`, and emits a comment heartbeat every 30
  seconds. Recognition errors retain PCM and emit a visible error event.
- [x] The history UI offers Pause, Resume, End and `Recover & pause`, shows
  durable chunk/transcript progress and committed final text, reconnects SSE,
  and warns before a full reload while capture or browser-queued PCM exists.
- [x] Thirty-three app backend tests, seven benchmark regression tests and
  eleven UI tests pass. The [Phase 4 validation record](evidence/local-meeting-phase4-2026-10-17.json)
  records the tested drain, replay, restart and UI contracts; Python static
  checks, TypeScript type-checking and the production UI build also pass.

### Phase 5 — export and release hardening

Add Markdown, text and JSON export, deletion, packaging, update checks, the
two-hour soak, fault matrix and privacy verification.

**Exit criteria:** every MVP acceptance criterion passes and the app can be
installed from a clean supported Mac without a Kiro Crew source checkout.

Phase 5 implementation and automated qualification completed on 2026-10-17;
the two wall-clock field gates below remain open, so this is not yet a release
sign-off:

- [x] Markdown, plain-text and versioned JSON exports contain committed final
  transcript segments and use deterministic safe filenames.
- [x] Deletion is explicitly confirmed, refuses active or draining meetings,
  isolates the meeting tree before the database commit, and recovers correctly
  on either side of that commit.
- [x] Covered PCM remains available for segment-level re-recognition. The UI
  exposes legacy `silence` coverage for manual retry but hides routine
  `empty_decode` rows from the final transcript. A successful retry atomically
  replaces the affected chunk-aligned transcript range. Schema v6 requeues the
  retained PCM for any meeting containing legacy `silence`, so the whole
  meeting is decoded again with full context because energy VAD is no longer
  allowed to suppress Whisper decoding.
- [x] The transcript groups nearby short segments into natural paragraphs when
  they share a 30-second interval and capture epoch and are separated by no
  more than two seconds. It adds a subtle `MM:SS` marker to the first speech
  paragraph in each interval. Elapsed audio time continues across capture
  epochs, so Pause and Resume do not reset the displayed timeline. Per-segment
  language remains in storage and exports but is hidden in the reading view.
- [x] The deterministic 38,640-byte release archive contains sixteen runtime
  files and has SHA-256
  `f41166dc22b90c290579d274c249829a74a68658c92e78893f2ed4907203ac1a`.
- [x] The manifest accepts Kiro Crew 0.10.0 and later. The archive installs and
  enables under Kiro Crew 0.10.0-insider.4 in an isolated home.
- [x] `requirements.txt` provisions `pywhispercpp>=1.5,<2` and the local OpenCC
  Simplified-Chinese converter into app-owned dependencies. Startup reports an
  actionable error when the Python package or native extension is absent, and
  model setup still refuses a CPU-only build.
  The clean 0.10 install provisioned pywhispercpp 1.5.1 and selected MTL0 on the
  tested Apple M4 Pro.
- [x] An extracted archive installs and enables in a clean isolated home. A real
  Gateway serves both UI artifacts, creates a meeting through the signed proxy,
  replaces the backend during an update, and preserves the record across that
  update and a full Gateway restart.
- [x] The accelerated two-hour soak stores 240 chunks and 115,200,000 samples,
  reaches exact final coverage, ends with no pending chunk or PCM object, and
  bounds its event log at 244 rows and SQLite file at 323,584 bytes.
- [x] The fault matrix covers upload retry, ACK mismatch, duplicate/conflicting
  delivery, spool/ledger crash recovery, worker death, drain restart, storage
  caps and deletion interruption. Recognition failure persists `last_error` and
  a `recognition.interrupted` event while the UI renders retry state and the
  retained-audio guarantee.
- [x] A runtime no-egress guard records zero socket and URL-open attempts during
  capture and recognition. The installed backend's process socket inventory has
  only its loopback listener and loopback Gateway connection; the only external
  runtime URL is the pinned model installer used before recording.
- [x] Sixty-four Python tests and nineteen UI tests pass; Python lint, macOS-targeted
  type checking, TypeScript checking, production UI build and production npm
  audit pass. See the [Phase 5 validation record](evidence/local-meeting-phase5-2026-10-17.json).
- [ ] Run one two-hour wall-clock capture through the Kiro Crew desktop page,
  including route-away/return and a backend restart, on the supported M4 Pro
  host. Accelerated PCM is not substituted for this field gate.
- [ ] Capture process-attributed network traffic during that real recording and
  Metal recognition run and verify that audio and transcript traffic remains on
  loopback. Socket guards and a point-in-time socket inventory do not substitute
  for the acceptance criterion's packet capture.

## Alternatives considered

### Continue patching the current Meetings app

Rejected for this MVP. It preserves the shared Dictation/STT lifecycle and makes
the reliability contract depend on behavior the new app does not need. Existing
Meetings fixes may still be valuable for that product; they are not the base of
this app.

### Extract a shared STT service first

Rejected as a prerequisite. A shared service broadens the design to multiple
clients and reintroduces coupling before the meeting durability contract is
proven. A later service extraction can use this app's ledger and drain semantics
as evidence.

### Send a continuous WebSocket stream

Rejected. A continuous stream makes the connection, rather than durable audio,
the centre of the design. IndexedDB plus idempotent HTTP chunks gives explicit
ownership, retry and acknowledgement boundaries. SSE is sufficient for progress.

### Write a native macOS application

Deferred. It would simplify system-audio capture and give tighter microphone
lifecycle control, but it would lose App Kit installation, navigation, backend
lifecycle and update integration. The microphone-only MVP does not require
native main-process code.

### Store one growing audio file

Rejected. Appending one file makes acknowledgement, retry, integrity checks and
partial recovery harder. Bounded content-verified chunks are easier to reconcile
after failure and can still be compacted after transcription.

## Open questions

1. Which existing GGML model locations are stable enough for read-only reuse, and
   when must the app keep its own verified copy under `data/models/`?
