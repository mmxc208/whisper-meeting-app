# Whisper Meeting

Whisper Meeting is an independent Kiro Crew App Kit app for durable, local-only
meeting transcription on Apple Silicon Macs. It does not share capture,
transport, session state, or recovery logic with the built-in Meetings app or
Chat Dictation.

Audio capture and Whisper inference stay local. The optional Note Taker and
Sketch Artist receive transcript text and Meeting Chat context through the
user's configured Kiro Crew ACP backend. With a cloud-backed provider such as
Claude, Kiro, or Codex, that text crosses the provider boundary; audio never
does, and the app adds no cloud endpoint of its own.

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
  --output dist/whisper-meeting-0.2.3.tar.gz
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
