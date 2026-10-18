# Whisper Meeting Agents MVP Plan

- **Status:** MVP implemented and live-tested; extended recovery matrix remains
- **Date:** 2026-10-17
- **Target app:** Whisper Meeting (`whisper-meeting`)
- **Target platform:** macOS on Apple Silicon, KiroCrew 0.10.0 or later

## 1. Decision summary

The existing Whisper Meeting capture and transcription MVP is the foundation and
is not redesigned by this plan. The implemented agent MVP adds three
capabilities:

1. a live **Note Taker**;
2. a live **Sketch Artist**;
3. a **Meeting Chat** that lets the user supply participants, agenda, terminology,
   corrections, and other context to every listening meeting agent.

The app remains independent from KiroCrew's built-in Meetings app and Chat
Dictation. It does not adopt the built-in Meetings STT, WebSocket, calendar,
task-review, or lifecycle implementation.

The two meeting agents run through the user's configured ACP backend. The ACP
harness is a local process, but its model may be remote: for example, the
configured Claude, Kiro, or Codex backend sends meeting text to its provider.
The app does not add an app-controlled cloud endpoint, choose a provider, or
silently fall back to a different backend.

### Current implementation status

| Phase | Status | Current evidence |
|---|---|---|
| A — platform proof | Complete | App agents, direct app-owned output, scoped `/api/spawn`, restart reconciliation, and secure rendering qualified |
| B — durable coordinator | Complete | Durable cursors, deterministic dispatch IDs, retry, and one-running/one-pending coalescing implemented |
| C — agents and secure outputs | Complete | Note Taker and Sketch Artist write versioned outputs; Mermaid/HTML is sanitized and sandboxed |
| D — meeting workspace | Complete | Notes, expanded Sketch, transcript, Meeting Chat, statuses, Enter-to-send, and exports are in the live UI |
| E — live qualification | Core path complete | Live meetings exercised Meeting Chat, agents, Pause/Resume, End, and exports; the combined route-away/reload/gateway-restart scenario remains |
| F — low-frequency coalescing | Complete | A deterministic 4:06 meeting with two context messages used two Note tasks and one Sketch task |

Version 0.3.3 moved full agent batches into durable input files to avoid the
provider's 5,000-character task limit. Version 0.3.4 made standalone Sketch
exports carry their Mermaid runtime inline, so exported diagrams render without
network access.

## 2. Goal

Turn the durable local transcript into useful live meeting artifacts without
weakening the recording path:

- structured notes that evolve during the meeting;
- a visual representation when the discussion contains a genuinely visual
  concept;
- an explicit channel for the user to correct or supplement what agents
  understand;
- reliable background finalization after the local Whisper backlog is drained,
  without making End wait for an agent turn.

## 3. Locked boundaries

The following existing behavior is frozen unless a defect blocks this MVP:

- browser microphone capture;
- 500 ms PCM chunking and durable IndexedDB queue;
- idempotent PCM upload and backend acknowledgement;
- local Metal Whisper recognition;
- pause/resume epochs;
- retained raw PCM and per-segment retry;
- the existing transcript and export data;
- the rule that recording continues independently of recognition or agent speed.

Agent work is downstream of the transcript. An agent failure must never stop,
pause, delay, or mutate audio capture or Whisper recognition.

## 4. User experience

### 4.1 Live meeting workspace

For the selected meeting, the primary workspace follows the useful information
architecture of KiroCrew's built-in Meetings app without importing its STT
architecture:

```text
+------------------------------------------------------------------+
| Meeting title | Recording / Paused / Ended      | Pause | End    |
+--------------------------------------+---------------------------+
| Notes | Sketch                       | Transcript                |
|                                      |                           |
| Current agent output                 | Speech and typed context  |
+--------------------------------------+---------------------------+
| Heard: latest speech                                             |
| Tell the meeting participants, agenda, or a correction...  Send |
+------------------------------------------------------------------+
```

- `Notes` and `Sketch` are tabs in the first MVP so both outputs do not compete
  for the same reading space.
- Transcript remains independently scrollable and keeps its current speech
  content, timestamps, and coverage behavior.
- The Meeting Chat remains visible while the meeting is recording.
- Agent status is explicit: `Starting`, `Listening`, `Working`, `Caught up`,
  `Finishing notes`, `Unavailable`, or `Failed`.

### 4.2 Meeting Chat

Meeting Chat accepts natural language. Example messages include:

- `Participants are Alice, Bob, and David.`
- `Today's agenda is cost, migration plan, and launch date.`
- `Kiro Cruise is a transcription error; the correct term is KiroCrew.`
- `The previous number was 50%, not 15%.`
- `Do not include the last aside in the meeting notes.`

Each accepted message:

1. is durably stored as a typed-context event;
2. appears in the meeting timeline with its own `Typed` treatment and timestamp;
3. is merged into each agent's next pending update with a chat/context marker;
4. remains distinguishable from speech in exports and diagnostics.

The UI acknowledges Meeting Chat immediately, but a message does not create its
own ACP task. Messages submitted close together are coalesced with speech and
with one another. This keeps context durable and prompt-visible without doubling
the task count for every correction or attendee update.

Typed context does not silently rewrite the immutable Whisper transcript. Agents
apply the correction to notes and diagrams while the original speech record stays
auditable.

### 4.3 Agent interaction deliberately excluded

This MVP does not implement a general agent builder. Users cannot add arbitrary
agent definitions, prompts, widget types, or output schemas. The roster is fixed
to Note Taker and Sketch Artist.

This MVP also does not implement a persistent, conversational chat thread with
one agent. Meeting Chat is an instruction/context channel, not a separate
assistant conversation. A later version may add a recipient selector or a full
per-agent chat surface without changing the broadcast contract defined here.

## 5. Agent behavior

### 5.1 Note Taker

The Note Taker maintains one complete Markdown document per meeting. It:

- organizes topics as they emerge;
- records participants and agenda when supplied;
- captures key points, decisions, action items, and open questions;
- paraphrases instead of copying the transcript;
- applies typed corrections and context;
- reads the current output before every update;
- rewrites the complete current document after every accepted batch;
- produces its first live revision no earlier than three minutes after recording
  begins, then no more often than every five minutes while new input exists;
- never invents an owner, deadline, participant, decision, or fact.

### 5.2 Sketch Artist

The Sketch Artist maintains one current self-contained HTML document per meeting.
It:

- draws architectures, workflows, relationships, timelines, and comparisons;
- uses Mermaid for diagrams and plain HTML/CSS for tables or comparisons;
- revises the current visual rather than accumulating versions;
- redraws only when a genuinely visual concept or relevant correction appears;
- produces no automatic live revision before five minutes and runs no more often
  than every ten minutes after that;
- applies typed corrections that change labels, entities, or relationships;
- performs no network request and references no remote asset.

### 5.3 Model and backend selection

Both agent specifications use `model: "auto"`. The app does not hardcode or
select a model ID. Agent turns run through the user's configured ACP backend and
therefore inherit that backend's provider and privacy characteristics.

If the configured ACP backend is unavailable:

- recording and Whisper transcription continue;
- pending agent batches remain durable;
- the UI marks agents unavailable and explains that transcription is unaffected;
- processing resumes from the durable cursor after the backend returns.

The UI and documentation must not describe the configured backend as local or
offline merely because its harness process runs on the Mac. They may claim that
meeting text stays on the Mac only when the user has selected and verified an
offline model runtime.

## 6. Architecture

### 6.1 App Kit integration

The manifest adds the two app-owned agent definitions and the minimum host API
grants required by the execution mode selected in Phase A. The app keeps
`permissions.network` disabled and uses only public App Kit APIs. It does not
import KiroCrew gateway internals or reuse the built-in Meetings backend.

Phase A evaluates the two supported execution modes, starting with the narrower
permission surface:

1. **Durable slots:** one deterministic slot per agent and meeting, using
   `/api/chat/slots` and `/api/chat`.
2. **Stateless dispatch:** one background task per batch using
   `dispatchAgentAsync` and `getTaskResult`; each task receives the new batch,
   all relevant typed-context events, the output path, and an instruction to
   read and rewrite the current output.

The MVP selects one mode from measured evidence. Stateless dispatch is selected
when it proves direct output, concurrent-agent execution, completion detection,
and restart reconciliation because it removes slot creation, slot resume, and
indeterminate slot-turn reconciliation. Durable slots are probed only if one of
those requirements fails; merely comparing an alternative does not justify the
broader `/api/chat` grants. Qualification records cold-start latency, completion
latency, token usage when reported, duplicate behavior, gateway-restart recovery,
and concurrent-agent behavior. Production never mixes both execution modes.

If durable slots are selected, the manifest grants exactly `/api/chat` and
`/api/chat/slots` in addition to app-owned backend routes. Stateless dispatch
keeps only the grants its public SDK calls require. Qualification records the
current `agent.approval_mode`; the app neither changes that setting nor bypasses
an approval policy. The current target configuration uses `auto`, so agent file
writes are expected to proceed unattended.

### 6.2 Data flow

```text
microphone
   -> existing durable PCM pipeline
   -> existing local Whisper recognition
   -> immutable speech segments
   -> agent dispatch ledger
        -> Note Taker execution   -> note-taker.md
        -> Sketch execution       -> sketch-artist.html

Meeting Chat
   -> typed-context event
   -> transcript timeline projection
   -> each agent's single coalesced pending update
```

Recognized speech and typed context are accumulated durably per agent. The Note
Taker becomes eligible for its first live update at three minutes and every five
minutes thereafter. The Sketch Artist becomes eligible at five minutes and no
more than every ten minutes thereafter. A timer never creates work when there is
no new input. The complete coalesced payload is materialized as an app-owned
dispatch input file; the provider task carries only the stable dispatch marker
and input/output paths. Provider task character limits therefore do not truncate
meeting content or create extra tasks.

Each agent owns at most one immutable running dispatch and one mutable pending
dispatch. New speech and context merge into the pending dispatch while a task is
running. Meeting End promotes that pending work into one final dispatch, folding
in all remaining speech, context, and finalization instructions. It must not
queue a speech task followed by a separate finalize task.

### 6.3 Additive persistence

New persistence is additive and separate from the audio/transcript tables.
Conceptually it contains:

#### `meeting_agent_state`

- `meeting_id`
- `agent_id`
- selected execution mode and execution reference;
- `state`
- `delivered_through_segment_id`
- `output_revision`
- `last_error`
- `updated_at`

#### `meeting_agent_dispatches`

- deterministic dispatch ID;
- meeting and agent IDs;
- kind: `update` or `finalize` (legacy rows may retain `speech` or `chat`);
- first and last speech segment IDs when applicable;
- payload hash;
- state: `pending`, `accepted`, `complete`, or `failed`;
- attempt count and last error.

#### `meeting_context_events`

- event ID;
- meeting ID;
- creation timestamp;
- original user text;
- delivery state for each agent.

No agent receives write access to PCM, audio chunk, recognition-job, or transcript
records.

### 6.4 Delivery and recovery

- The coordinator allocates a durable dispatch before sending it.
- Each send carries a deterministic correlation ID.
- A positive gateway receipt advances the delivery cursor.
- An indeterminate response is reconciled through the selected execution
  mode's status API before retry, rather than blindly issuing duplicate work.
- Duplicate transcript delivery must be harmless: agents rewrite complete
  outputs and receive stable dispatch IDs.
- Each agent has at most one running task and one coalesced pending task per
  meeting, preventing concurrent rewrites and unbounded task queues.
- Route changes and page reloads reconstruct pending work from the backend
  ledger and task or slot status, not from React component state.
- Agent turns may lag arbitrarily far behind recording without stopping capture.

## 7. Meeting lifecycle

### Start

1. Start the existing capture path immediately.
2. Initialize the selected agent execution mode concurrently.
3. Initialize output files and prompts in the background.
4. Buffer recognized segments durably while an agent starts.

Agent startup is never on the critical path for microphone capture.

### Recording

- Append speech to the existing transcript.
- Merge new visible speech and typed context into the agent's pending update.
- Dispatch only when that agent's minimum cadence has elapsed.
- Poll or stream agent state and output revisions into the UI.

### Pause

- Preserve the existing audio flush and exact cut behavior.
- Preserve complete transcript/context input in the coalesced pending update;
  Pause does not bypass the agent cadence.
- Disable Meeting Chat while the meeting ingress is paused.
- Keep agent execution and output available for reading.

### Resume

- Preserve the existing new capture epoch behavior.
- Continue agent delivery from the existing transcript cursor.
- Do not reinitialize or duplicate already accepted agent work.

### End

1. Preserve the existing audio stop and Whisper drain.
2. Merge remaining speech, typed context, and finalization instructions into at
   most one pending final dispatch per agent without waiting for either to run.
3. If an agent task is already running, keep exactly one final dispatch behind
   it; otherwise dispatch the final work immediately.
4. Mark the meeting `Ended` as soon as those records are committed.
5. Let the coordinator deliver and reconcile the agent work in the background.
6. Show `Finishing notes` or `Finishing sketch` until background work completes;
   keep the meeting and partial outputs readable throughout.

There is no task-review or `Reviewing` lifecycle state in this MVP. If an agent is
slow or unavailable, `Agent output pending` is a normal recoverable state rather
than an End failure. Processing resumes from the durable dispatch ledger when the
backend returns.

## 8. Output storage and rendering

Preferred output paths are app-owned files under the meeting's existing data
directory:

```text
sessions/<meeting-id>/agents/note-taker.md
sessions/<meeting-id>/agents/sketch-artist.html
```

The first technical probe must prove that a third-party app agent can safely
write these paths under the current KiroCrew version. Direct `fs_read` and
`fs_write` are the MVP design; a `save_output` MCP server is not implemented as
a speculative fallback. If direct writes fail, Phase A stops for an explicit
design decision rather than importing private KiroCrew modules or widening
filesystem access.

The agent prompts and `OUTPUT_FILE` contract start from KiroCrew's built-in
`meetings-note-taker.json`, `meetings-sketch-artist.json`, and Meetings session
contract. The independent app preserves the required Apache-2.0 attribution and
adapts only the app-specific path and lifecycle wording.

Markdown should use `MarkdownRenderer` from `@kirocrew/app-sdk/ui`. Current
KiroCrew main exports it, but Phase A verifies that the installed 0.10.0 host
provides the runtime export before the MVP depends on it.

Sketch HTML is untrusted model output. It renders only after sanitization inside
a null-origin iframe with:

- `sandbox="allow-scripts"` and no `allow-same-origin`;
- a CSP that denies network egress;
- scripts, event handlers, navigation, speculative loads, remote images, and
  remote fonts removed;
- only the host-vendored Mermaid runtime started by app-authored bootstrap code.

The app owns its Sketch `srcdoc` construction unless Phase A identifies a public
App Kit builder. The built-in Mochi `buildSrcdoc` module is a useful security
reference but is not a public app-sdk import.

PCM retention may remove only files referenced by retained audio-chunk rows. It
must leave `sessions/<meeting-id>/agents/` and its outputs untouched. Explicit
meeting deletion removes the complete meeting tree, including agent outputs.

## 9. Privacy and security contract

- Raw audio and Whisper inference remain on the Mac.
- Audio is never included in an agent prompt.
- Agents receive only finalized speech text, typed context, meeting metadata, and
  their own existing output.
- Transcript text is sent to the user's configured ACP provider. With Claude,
  Kiro, or Codex this normally means a cloud model; a locally running harness is
  not itself an offline-model guarantee.
- The app adds no cloud endpoint, makes no direct external network request, and
  requests no app network permission.
- The app never selects or falls back to a different ACP backend. Backend
  unavailability leaves agent work pending while recording and transcription
  remain usable.
- Typed context is untrusted data, not an instruction that may change app or
  agent security policy.
- Model-generated Markdown and HTML are sanitized before rendering.
- Agent output cannot modify the source transcript or audio ledger.

## 10. Technical probe

Implementation begins with a narrow proof on the currently running KiroCrew
0.10.0 build. It must verify:

1. manifest-declared third-party agents register and materialize correctly;
2. manifest permissions admit the minimum APIs required by each candidate
   execution mode and reject undeclared APIs;
3. an app agent can directly read and write only its assigned app-owned output;
4. Note Taker and Sketch Artist can run concurrently through the configured ACP
   backend;
5. stateless async dispatch is qualified for output coherence, latency, token
   reporting, duplicate handling, and restart recovery; durable slots are tested
   only if stateless dispatch fails a requirement;
6. accepted, queued, failed, and indeterminate work can be reconciled without
   duplicate output in the selected mode;
7. the app can determine when an agent turn or task has finished;
8. route-away, page reload, and gateway restart recover pending work;
9. the installed host exports a usable App Kit `MarkdownRenderer`;
10. sanitized Sketch HTML renders Mermaid in a null-origin iframe with zero
    external network traffic;
11. PCM retention deletes expired spool files without deleting either agent
    output, while explicit meeting deletion removes all three;
12. agent failure leaves recording, transcription, Pause/Resume, and End
    unchanged.

The probe produces a small machine-readable evidence file and a written go/no-go
decision that selects one execution mode and confirms direct file output. It
also records whether the configured backend uses an offline or remote model; it
does not change the production capture path.

### 10.1 Phase A evidence — 2026-10-18

The temporary `0.2.0-probe.1` build was synced into the running KiroCrew
`0.10.0-insider.4` installation. The user approved exactly `/api/spawn`; the
existing app-owned API grant remained unchanged and `/api/chat/slots` was
confirmed to return HTTP 403 for the app token.

- Both manifest agents materialized under the app namespace.
- The configured ACP backend was `claude`, so the synthetic transcript text used
  by this probe crossed the configured provider boundary; no audio was sent.
- Note Taker and Sketch Artist were accepted concurrently and both completed in
  60.491 seconds. They made tool calls and directly wrote their assigned
  app-owned outputs (732-byte Markdown and 2,532-byte HTML).
- The bilingual synthetic content was represented correctly in both outputs.
- The installed host exports `MarkdownRenderer` from its app UI vendor module.
- After a host page reload, the synced Meeting workspace exposed the Notes,
  Sketch, transcript-export, combined-export, and Meeting Chat controls while
  preserving the existing transcript view.
- The real generated Sketch passed the server sanitizer: scripts, iframes, and
  remote URLs were absent; `default-src 'none'` and `connect-src 'none'` were
  present; Mermaid source remained available for the app-owned renderer.
- The production coordinator was enabled only for newly created meetings. The
  nine retained meetings remained untouched with zero agent-state, dispatch,
  and typed-context rows.
- A pending two-agent batch was accepted, the KiroCrew gateway was restarted,
  and the generation-scoped task IDs correctly became unavailable. Recovery
  reused both original durable dispatch IDs, incremented each attempt count to
  two, and produced exactly one idempotency marker in each final output.
- The recovered Note Taker and Sketch Artist tasks completed independently and
  wrote 659-byte Markdown and 2,003-byte HTML outputs. The recovered Sketch
  again passed the server sanitizer with no script, iframe, or remote URL.
- Stateless spawn now explicitly requests the already-qualified `auto` approval
  mode. This is required for unattended app dispatch; the app does not change
  the user's global approval setting.
- PCM retention tests exercise both files under the real agent-output directory
  and confirm that expiry removes only PCM. Explicit meeting deletion removes
  the complete meeting tree.
- The synced workspace rendered Sketch inside `about:srcdoc`; the iframe has only
  `allow-scripts`, no same-origin capability, and its CSP allows only the
  host-vendored Mermaid runtime while denying connections.
- Backend tests (90), frontend tests (23), TypeScript checking, Python formatting
  and import-order checks, and the production UI build passed after the final
  restart and retention changes.

The first normal meeting on 2026-10-18 exposed two production-only defects that
the synthetic probe did not cover. KiroCrew published the gateway origin as
`localhost`; the backend obtained an app token but Python's manually constructed
domain cookie did not return it, so every `/api/spawn` lookup received HTTP 403.
Version 0.2.1 sends the in-memory app token explicitly as the gateway cookie and
adds a localhost regression test. The durable queue recovered the original
dispatch IDs after sync without losing transcript data.

The same meeting also showed that agent state was stale in the workspace after
background completion because agent-file writes do not emit meeting SSE events.
Version 0.2.2 polls only while an agent is non-terminal and stops after every
agent is complete or failed. The 485-chunk meeting ended with exact 485/485
coverage; Note Taker and Sketch Artist each completed ten durable dispatches,
reached output revision 10, and covered the final transcript segment. The final
Notes incorporated the typed topic and attendee context, and the final Sketch
rendered in the sandboxed iframe. Full Phase E remains open because this meeting
did not exercise Pause/Resume.

That meeting lasted 4 minutes 6 seconds and exposed an unacceptable task-cost
shape: each agent ran seven 30-second speech tasks, two independent Meeting Chat
tasks, and one finalize task, for 20 Claude tasks total. Version 0.2.3 also proved
that an expired app-scoped gateway token can be reauthenticated without losing
the durable queue. The next optimization keeps that recovery model but replaces
30-second dispatch and per-message tasks with the coalesced cadence in section
6.2. For a meeting under five minutes with two context messages, the acceptance
budget is two Note Taker tasks and one Sketch Artist task by default, or at most
four total when an explicit visual update is requested.

Version 0.3.0 implements that low-frequency coordinator. A deterministic
4-minute-6-second qualification with two context messages produced exactly two
Note tasks and one Sketch task. After sync into the running KiroCrew instance, a
short acceptance meeting stored one Meeting Chat event without creating a task;
End then created exactly one final task per agent. Both completed with 52/52
audio chunks covered, and the acceptance meeting was removed after validation.

A later live meeting exposed the provider's 5,000-character task limit when a
Sketch batch reached 5,946 characters. Version 0.3.3 moves complete dispatch
payloads into durable app-owned input files and keeps gateway task prompts short,
so large coalesced batches remain lossless without increasing task frequency.

**Decision:** GO with stateless dispatch and direct app-owned output files. Do
not grant `/api/chat` or `/api/chat/slots`. Phase A is complete: the real restart
qualification proves durable reconciliation without duplicate output, the
sanitizer plus null-origin iframe CSP closes Sketch network and host-document
access, and retention is pinned for both output files. The production coordinator
is enabled for new meetings only. Phase E remains intentionally open until the
user runs a normal bilingual meeting with Pause/Resume and Meeting Chat.

## 11. Implementation phases

### Phase A — platform proof

- Add temporary agent definitions and minimum manifest permissions.
- Prove direct app-owned output reads and writes.
- Qualify stateless async dispatch; test durable slots only if it fails a
  requirement.
- Record the configured backend's actual privacy boundary without changing it.
- Verify App Kit Markdown export on the installed host.
- Prove secure Sketch rendering.
- Lock PCM retention behavior with an agent-output survival test.

**Exit:** all technical-probe items have evidence, stateless dispatch is selected
or a measured failure justifies durable-slot grants, and direct output writes are
confirmed.

### Phase B — durable coordinator

- Add agent state, dispatch ledger, and typed-context persistence.
- Implement deterministic batching, receipts, reconciliation, and cursor resume.
- Add backend endpoints for agent state, pending dispatches, context, and outputs.
- Add unit tests for reload, retry, duplicate receipt, long backlog, and one-agent
  failure.

**Exit:** transcript and chat delivery survive reload and gateway restart without
affecting capture.

### Phase C — agents and secure outputs

- Ship fixed Note Taker and Sketch Artist agent specifications.
- Adapt the built-in Meetings prompts and `OUTPUT_FILE` contract, preserving
  Apache-2.0 attribution.
- Implement complete Markdown and HTML output updates.
- Implement Markdown rendering and hardened Sketch iframe rendering.
- Add finalization and output revision tracking.

**Exit:** both agents update live and finalize from a deterministic test
transcript.

### Phase D — meeting workspace

- Add Notes/Sketch tabs beside the transcript.
- Add sticky Meeting Chat and typed-context timeline rows.
- Add explicit agent status and recovery messaging.
- Add notes, sketch, and combined export actions.
- Verify narrow and wide macOS desktop layouts.

**Exit:** the complete workflow is usable without opening Sessions or the
built-in Meetings app.

### Phase E — live qualification

- Run a real bilingual meeting with Pause/Resume.
- Supply participants, agenda, vocabulary, and a factual correction through
  Meeting Chat.
- Include one genuinely visual workflow and one non-visual discussion.
- Route away and return, reload the page, and restart the gateway once.
- End the meeting while one final transcript batch remains.
- Confirm exact transcript coverage and finalized outputs.

**Exit:** every acceptance item below passes with saved evidence.

### Phase F — low-frequency coalescing

- Replace 30-second allocation with the per-agent cadence in section 6.2.
- Keep one mutable pending update behind at most one immutable running task.
- Coalesce Meeting Chat with speech rather than spawning per-message tasks.
- Fold remaining input and finalize instructions into one End dispatch.
- Preserve restart, token-expiry, and deterministic-ID recovery.

**Exit:** a four-to-five-minute qualification meeting with two context messages
finishes with three tasks by default and never exceeds four without losing any
speech segment or typed context.

## 12. Acceptance checklist

### Existing recording path

- [ ] Audio and transcript chunk counts match the existing baseline.
- [ ] Note/Sketch failures cannot change capture or recognition state.
- [ ] Pause/Resume retains exact transcript coverage across epochs.
- [ ] Agent backlog never stops recording.

### Note Taker

- [ ] No live Note task starts before three minutes of an active meeting.
- [ ] The first Note update is eligible at three minutes and later updates are
      separated by at least five minutes while new input exists.
- [ ] Participants and agenda supplied through Chat appear in notes.
- [ ] A terminology or factual correction updates notes without rewriting the raw
      transcript.
- [ ] Notes contain no invented participant, decision, owner, or deadline.
- [ ] The finalized Markdown remains available after reload and restart.

### Sketch Artist

- [ ] A workflow or relationship produces a useful diagram.
- [ ] Ordinary non-visual discussion does not force a redraw.
- [ ] A typed correction affecting a node or relationship updates the diagram.
- [ ] Generated HTML has no network access and cannot access the host document.
- [ ] The finalized Sketch remains available after reload and restart.

### Meeting Chat

- [ ] Every submitted message is durably stored before dispatch.
- [ ] Typed messages appear with timestamp and a distinct source treatment.
- [ ] Messages merge into both agents' next pending update without creating a
      dedicated task per message.
- [ ] An unavailable agent does not prevent delivery to the other agent.
- [ ] Failed or indeterminate delivery remains visible and retryable.

### Lifecycle and recovery

- [ ] Recording starts without waiting for agents.
- [ ] Route-away and reload catch up from durable cursors.
- [ ] Gateway restart does not duplicate accepted batches.
- [ ] End returns after audio and Whisper drain without waiting for either agent.
- [ ] Remaining transcript and finalize work continues durably after End.
- [ ] `Finishing notes` and `Finishing sketch` resolve independently to a final
      or actionable failed state.
- [ ] No task-review or stuck `Reviewing` state is introduced.
- [ ] Each agent has at most one running and one pending dispatch.
- [ ] End produces at most one additional final dispatch per agent.
- [ ] A four-to-five-minute meeting with two context messages uses three tasks by
      default and at most four when an explicit visual update is requested.
- [ ] Configured ACP-backend unavailability leaves transcription usable and
      explains the degraded agent state.
- [ ] The app does not switch to a different backend after failure.

### Storage retention

- [ ] Expired PCM cleanup removes only retained spool files and updates their
      ledger rows.
- [ ] PCM cleanup preserves finalized and in-progress agent output files.
- [ ] Explicit meeting deletion removes transcript, PCM, dispatch state, and
      agent outputs together.

### Export

- [ ] Transcript export preserves speech and typed-context provenance.
- [ ] Notes export produces Markdown.
- [ ] Sketch export produces self-contained sanitized HTML.
- [ ] A combined export identifies the meeting, agent revision, and finalization
      status.

## 13. Deferred work

- arbitrary user-configured meeting agents;
- persistent one-to-one chat with an individual agent;
- task extraction and action-item review;
- calendar integration, attachments, and presets;
- speaker diarization;
- choosing, configuring, or overriding the user's ACP provider;
- automatic rewriting of the source transcript from typed corrections.

