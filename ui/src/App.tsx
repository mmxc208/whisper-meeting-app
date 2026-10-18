import { fmtDateTime, useAppApi } from '@kirocrew/app-sdk'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Btn,
  Card,
  CardTitle,
  EmptyState,
  ErrorNotice,
  Input,
  MarkdownRenderer,
  PageHeader,
  StatCard,
} from '@kirocrew/app-sdk/ui'
import { useEffect, useState, useSyncExternalStore } from 'react'
import {
  NOTE_AGENT,
  SKETCH_AGENT,
  agentSnapshotNeedsPolling,
  agentStateLabel,
} from './agentStatus'
import { captureEngine, captureSnapshot, subscribeCapture } from './captureEngine'
import { buildSketchSrcdoc } from './sketchSrcdoc'
import { visibleTranscriptRows } from './transcript'

type Meeting = {
  id: string
  title: string
  state: string
  created_at: string
  updated_at: string
  last_error: string
}

type MeetingList = { meetings: Meeting[] }
type MeetingResult = { meeting: Meeting }
type MeetingProgress = {
  meeting: Meeting
  capture: {
    epoch: number
    active: boolean
    cut_sequence: number
    cut_sample: number
  } | null
  operation: { kind: string; state: string } | null
  progress: {
    chunks: number
    transcribed_chunks: number
    pending_chunks: number
    spool_bytes: number
  }
}
type TranscriptResult = {
  meeting: Meeting
  segments: Array<{
    id: number
    epoch: number
    audio_start_sample: number
    audio_end_sample: number
    language: string
    text: string
    coverage_kind: 'speech' | 'silence' | 'empty_decode'
  }>
}
type ModelStatus = {
  status: 'missing' | 'installing' | 'ready' | 'failed'
  model: string
  size_bytes: number
  error: string
}
type ModelResult = { model: ModelStatus }
type AgentState = {
  agent_id: string
  state: string
  output_revision: number
  last_error: string
}
type AgentResult = {
  agents: AgentState[]
  context: Array<{ id: string; text: string; created_at: string }>
  dispatch_counts: Array<{ agent_id: string; state: string; count: number }>
  dispatches: Array<{
    id: string
    agent_id: string
    kind: string
    state: string
    attempt_count: number
    last_error: string
  }>
  outputs: Record<string, string>
}
const BACKEND_API = '/apps/whisper-meeting/api'

export default function WhisperMeeting() {
  const api = useAppApi()
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [selectedId, setSelectedId] = useState<string>()
  const [actionError, setActionError] = useState('')
  const [retryingSegmentId, setRetryingSegmentId] = useState<number>()
  const [retryingAgentId, setRetryingAgentId] = useState<string>()
  const [agentTab, setAgentTab] = useState<'notes' | 'sketch'>('notes')
  const [meetingContext, setMeetingContext] = useState('')
  const capture = useSyncExternalStore(subscribeCapture, captureSnapshot)

  useEffect(() => {
    void captureEngine.attach(api)
  }, [api])

  useEffect(() => {
    const warnOnExit = (event: BeforeUnloadEvent) => {
      if (capture.phase === 'recording' || capture.queuedChunks > 0) event.preventDefault()
    }
    window.addEventListener('beforeunload', warnOnExit)
    return () => window.removeEventListener('beforeunload', warnOnExit)
  }, [capture.phase, capture.queuedChunks])

  const meetings = useQuery({
    queryKey: ['meetings'],
    queryFn: () => api.get<MeetingList>(`${BACKEND_API}/meetings`),
  })
  const model = useQuery({
    queryKey: ['whisper-meeting-model'],
    queryFn: () => api.get<ModelResult>(`${BACKEND_API}/model`),
    refetchInterval: query => query.state.data?.model.status === 'installing' ? 1000 : false,
  })
  const detail = useQuery({
    queryKey: ['meeting', selectedId],
    queryFn: () => api.get<MeetingProgress>(
      `${BACKEND_API}/meetings/${encodeURIComponent(selectedId ?? '')}`,
    ),
    enabled: Boolean(selectedId),
  })
  const transcript = useQuery({
    queryKey: ['meeting-transcript', selectedId],
    queryFn: () => api.get<TranscriptResult>(
      `${BACKEND_API}/meetings/${encodeURIComponent(selectedId ?? '')}/transcript`,
    ),
    enabled: Boolean(selectedId),
  })
  const agents = useQuery({
    queryKey: ['meeting-agents', selectedId],
    queryFn: () => api.get<AgentResult>(
      `${BACKEND_API}/meetings/${encodeURIComponent(selectedId ?? '')}/agents`,
    ),
    enabled: Boolean(selectedId),
    refetchInterval: query => agentSnapshotNeedsPolling(query.state.data?.agents ?? [])
      ? 1000
      : false,
  })
  const installModel = useMutation({
    mutationFn: () => api.post<ModelResult>(`${BACKEND_API}/model/install`, {}),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['whisper-meeting-model'] })
    },
  })
  const createMeeting = useMutation({
    mutationFn: (meetingTitle: string) =>
      api.post<MeetingResult>(`${BACKEND_API}/meetings`, { title: meetingTitle }),
    onSuccess: async () => {
      setTitle('')
      await queryClient.invalidateQueries({ queryKey: ['meetings'] })
    },
  })
  const sendMeetingContext = useMutation({
    mutationFn: (text: string) => api.post(
      `${BACKEND_API}/meetings/${encodeURIComponent(selectedId ?? '')}/context`,
      { text },
    ),
    onSuccess: async () => {
      setMeetingContext('')
      await queryClient.invalidateQueries({ queryKey: ['meeting-agents', selectedId] })
    },
  })

  useEffect(() => {
    if (!selectedId) return
    const events = new EventSource(
      `${BACKEND_API}/meetings/${encodeURIComponent(selectedId)}/events`,
    )
    events.onmessage = () => {
      void queryClient.invalidateQueries({ queryKey: ['meetings'] })
      void queryClient.invalidateQueries({ queryKey: ['meeting', selectedId] })
      void queryClient.invalidateQueries({ queryKey: ['meeting-transcript', selectedId] })
      void queryClient.invalidateQueries({ queryKey: ['meeting-agents', selectedId] })
    }
    return () => events.close()
  }, [queryClient, selectedId])

  useEffect(() => {
    if (detail.data) {
      captureEngine.markServerState(detail.data.meeting.id, detail.data.meeting.state)
    }
  }, [detail.data])

  async function runAction(meetingId: string, action: () => Promise<void>): Promise<void> {
    setSelectedId(meetingId)
    setActionError('')
    try {
      await action()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['meetings'] }),
        queryClient.invalidateQueries({ queryKey: ['meeting', meetingId] }),
        queryClient.invalidateQueries({ queryKey: ['meeting-transcript', meetingId] }),
        queryClient.invalidateQueries({ queryKey: ['meeting-agents', meetingId] }),
      ])
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error))
    }
  }

  async function downloadExport(meetingId: string, format: string): Promise<void> {
    setActionError('')
    try {
      const response = await api.raw(
        `${BACKEND_API}/meetings/${encodeURIComponent(meetingId)}/export?format=${format}`,
      )
      const disposition = response.headers.get('Content-Disposition') ?? ''
      const filename = disposition.match(/filename="([^"]+)"/)?.[1]
        ?? `meeting.${format === 'text' ? 'txt' : format === 'markdown' ? 'md' : 'json'}`
      const objectUrl = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = objectUrl
      link.download = filename
      link.click()
      URL.revokeObjectURL(objectUrl)
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error))
    }
  }

  async function deleteMeeting(meeting: Meeting): Promise<void> {
    const label = meeting.title || 'Untitled meeting'
    if (!window.confirm(`Permanently delete “${label}” and its local data?`)) return
    setActionError('')
    try {
      await api.del(`${BACKEND_API}/meetings/${encodeURIComponent(meeting.id)}`)
      if (selectedId === meeting.id) setSelectedId(undefined)
      await queryClient.invalidateQueries({ queryKey: ['meetings'] })
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error))
    }
  }

  async function retryCoverage(segmentId: number): Promise<void> {
    if (!selectedId) return
    setActionError('')
    setRetryingSegmentId(segmentId)
    try {
      await api.post(
        `${BACKEND_API}/meetings/${encodeURIComponent(selectedId)}/segments/${segmentId}/retry`,
        {},
      )
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['meeting', selectedId] }),
        queryClient.invalidateQueries({ queryKey: ['meeting-transcript', selectedId] }),
      ])
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error))
    } finally {
      setRetryingSegmentId(undefined)
    }
  }

  async function retryAgent(agentId: string): Promise<void> {
    if (!selectedId) return
    setActionError('')
    setRetryingAgentId(agentId)
    try {
      await api.post(
        `${BACKEND_API}/meetings/${encodeURIComponent(selectedId)}`
        + `/agents/${encodeURIComponent(agentId)}/retry`,
        {},
      )
      await queryClient.invalidateQueries({ queryKey: ['meeting-agents', selectedId] })
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error))
    } finally {
      setRetryingAgentId(undefined)
    }
  }

  const rows = meetings.data?.meetings ?? []
  const recognitionFailure = rows.find(meeting => meeting.last_error)
  const error = meetings.error ?? model.error ?? createMeeting.error ?? installModel.error
    ?? detail.error ?? transcript.error ?? agents.error ?? sendMeetingContext.error
  const errorMessage = error instanceof Error ? error.message : String(error ?? '')

  return (
    <>
      <PageHeader
        title="Whisper Meeting"
        subtitle="Durable, local-first meeting transcription"
      />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Meetings" value={String(rows.length)} accent />
          <StatCard label="Storage" value="Local" />
          <StatCard label="Whisper model" value={model.data?.model.status ?? 'Checking'} />
          <StatCard label="Capture" value={capture.phase} />
          <StatCard label="Upload queue" value={String(capture.queuedChunks)} />
        </div>

        {error ? (
          <div className="mb-4">
            {/* No hand-off: the unsaved meeting title is held in this subtree. */}
            <ErrorNotice
              title="Whisper Meeting could not load"
              message={errorMessage}
            />
          </div>
        ) : null}

        {capture.fatalError || capture.uploadError ? (
          <div className="mb-4">
            {/* No hand-off: the unsaved meeting title is held in this subtree. */}
            <ErrorNotice
              title={capture.fatalError ? 'Capture stopped' : 'Upload will retry'}
              message={capture.fatalError ?? capture.uploadError}
            />
          </div>
        ) : null}

        {recognitionFailure ? (
          <div className="mb-4">
            <ErrorNotice
              title="Recognition is waiting to recover"
              message={`${recognitionFailure.title || 'Untitled meeting'}: ${recognitionFailure.last_error}`}
            />
          </div>
        ) : null}

        {actionError ? (
          <div className="mb-4">
            <ErrorNotice title="Meeting action failed" message={actionError} />
          </div>
        ) : null}

        {capture.phase === 'recording' ? (
          <Card className="mb-4">
            <CardTitle>Recording locally</CardTitle>
            <p className="text-sm text-muted mt-2">
              Audio is committed to IndexedDB before upload. Closing this page does not stop the module-level capture engine.
            </p>
            <div className="mt-3 flex gap-2">
              <Btn onClick={() => {
                if (capture.meetingId) {
                  void runAction(capture.meetingId, () => captureEngine.pause(api))
                }
              }}>
                Pause
              </Btn>
              <Btn onClick={() => {
                if (capture.meetingId) {
                  void runAction(capture.meetingId, () => captureEngine.end(api))
                }
              }}>
                End
              </Btn>
            </div>
          </Card>
        ) : null}

        {['stopping', 'uploading', 'transcribing', 'recovering'].includes(capture.phase) ? (
          <Card className="mb-4">
            <CardTitle>Finishing the durable cut</CardTitle>
            <p className="text-sm text-muted mt-2">
              {capture.phase === 'transcribing'
                ? 'All captured audio is durable. Waiting for final transcript coverage.'
                : 'Keeping the meeting open until every local chunk has a durable backend ACK.'}
            </p>
          </Card>
        ) : null}

        {model.data?.model.status !== 'ready' ? (
          <Card className="mb-4">
            <CardTitle>Local Whisper model</CardTitle>
            <p className="text-sm text-muted mt-2">
              Whisper Meeting keeps its own verified large-v3-turbo model so Kiro Crew updates cannot replace it.
            </p>
            {model.data?.model.error ? (
              <p className="text-sm text-danger mt-2">{model.data.model.error}</p>
            ) : null}
            <div className="mt-3">
              <Btn
                onClick={() => installModel.mutate()}
                disabled={model.data?.model.status === 'installing' || installModel.isPending}
              >
                {model.data?.model.status === 'installing' ? 'Installing and testing…' : 'Install local model'}
              </Btn>
            </div>
          </Card>
        ) : null}

        <Card className="mb-4">
          <CardTitle>New meeting</CardTitle>
          <div className="flex flex-col md:flex-row gap-3 mt-3">
            <Input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Optional meeting title"
              aria-label="Meeting title"
              maxLength={200}
            />
            <Btn
              onClick={() => createMeeting.mutate(title)}
              disabled={createMeeting.isPending}
            >
              {createMeeting.isPending ? 'Creating…' : 'Create meeting'}
            </Btn>
          </div>
        </Card>

        <Card>
          <CardTitle>Meeting history</CardTitle>
          {meetings.isPending ? (
            <p className="text-sm text-muted mt-3">Loading meetings…</p>
          ) : rows.length === 0 ? (
            <EmptyState
              icon={<span aria-hidden="true">🎙️</span>}
              title="No meetings yet"
              subtitle="Create a meeting to verify the durable app store."
            />
          ) : (
            <div className="divide-y divide-border mt-3">
              {rows.map((meeting) => (
                <div key={meeting.id} className="py-3 first:pt-0 last:pb-0">
                  <div className="flex items-center justify-between gap-3">
                    <div className="font-medium text-text">
                      {meeting.title || 'Untitled meeting'}
                    </div>
                    <div className="flex gap-2">
                      <Btn onClick={() => setSelectedId(meeting.id)}>View</Btn>
                      {meeting.state === 'draft' && capture.phase !== 'recording' ? (
                        <Btn
                          onClick={() => void runAction(
                            meeting.id,
                            () => captureEngine.start(api, meeting.id),
                          )}
                          disabled={capture.phase === 'starting' || model.data?.model.status !== 'ready'}
                        >
                          Record
                        </Btn>
                      ) : null}
                      {meeting.state === 'paused' && capture.phase !== 'recording' ? (
                        <>
                          <Btn
                            onClick={() => void runAction(
                              meeting.id,
                              () => captureEngine.start(api, meeting.id, true),
                            )}
                            disabled={capture.phase === 'starting' || model.data?.model.status !== 'ready'}
                          >
                            Resume
                          </Btn>
                          <Btn onClick={() => void runAction(
                            meeting.id,
                            () => captureEngine.recover(api, meeting.id, 'end'),
                          )}>
                            End
                          </Btn>
                        </>
                      ) : null}
                      {meeting.state === 'recording'
                        && capture.meetingId !== meeting.id ? (
                          <Btn onClick={() => void runAction(
                            meeting.id,
                            () => captureEngine.recover(api, meeting.id, 'pause'),
                          )}>
                            Recover &amp; pause
                          </Btn>
                        ) : null}
                      {!['recording', 'draining'].includes(meeting.state) ? (
                        <Btn onClick={() => void deleteMeeting(meeting)}>Delete</Btn>
                      ) : null}
                    </div>
                  </div>
                  <div className="text-sm text-muted mt-1">
                    {meeting.state} · {fmtDateTime(meeting.created_at)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {selectedId ? (
          <Card className="mt-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <CardTitle>Meeting workspace</CardTitle>
                {detail.data ? (
                  <p className="text-sm text-muted mt-2">
                    {detail.data.meeting.state} · {detail.data.progress.transcribed_chunks}/
                    {detail.data.progress.chunks} chunks covered
                    {` · ${detail.data.progress.pending_chunks} awaiting recognition`}
                    {detail.data.operation
                      ? ` · ${detail.data.operation.kind} ${detail.data.operation.state}`
                      : ''}
                  </p>
                ) : null}
              </div>
              <div className="flex flex-wrap gap-2 text-xs text-muted">
                {(agents.data?.agents ?? []).map(agent => (
                  <span key={agent.agent_id} className="rounded-full border border-border px-2 py-1">
                    {agent.agent_id === NOTE_AGENT ? 'Note Taker' : 'Sketch Artist'}:
                    {' '}{agentStateLabel(agent, detail.data?.meeting.state)}
                    {(() => {
                      const queued = (agents.data?.dispatch_counts ?? [])
                        .filter(row => row.agent_id === agent.agent_id && row.state === 'pending')
                        .reduce((total, row) => total + row.count, 0)
                      return queued ? ` · ${queued} queued` : ''
                    })()}
                  </span>
                ))}
              </div>
            </div>

            {(agents.data?.agents ?? []).filter(agent => agent.state === 'failed').map(agent => (
              <div
                key={`${agent.agent_id}-failure`}
                className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-danger/40 px-3 py-2"
              >
                <p className="text-sm text-danger">
                  {agent.agent_id === NOTE_AGENT ? 'Note Taker' : 'Sketch Artist'} failed
                  {agent.last_error ? `: ${agent.last_error}` : '.'}
                  {' '}Transcription is unaffected.
                </p>
                <Btn
                  onClick={() => void retryAgent(agent.agent_id)}
                  disabled={retryingAgentId !== undefined}
                >
                  {retryingAgentId === agent.agent_id ? 'Retrying…' : 'Retry agent'}
                </Btn>
              </div>
            ))}

            {detail.data?.meeting.last_error ? (
              <p className="text-sm text-danger mt-2">
                Recognition will retry without deleting audio: {detail.data.meeting.last_error}
              </p>
            ) : null}

            <div className="mt-4 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.9fr)]">
              <section className="min-w-0">
                <div className="flex gap-2 border-b border-border pb-2">
                  <Btn onClick={() => setAgentTab('notes')} disabled={agentTab === 'notes'}>
                    Notes
                  </Btn>
                  <Btn onClick={() => setAgentTab('sketch')} disabled={agentTab === 'sketch'}>
                    Sketch
                  </Btn>
                  <Btn onClick={() => void downloadExport(selectedId, 'notes')}>Export notes</Btn>
                  <Btn onClick={() => void downloadExport(selectedId, 'sketch')}>Export sketch</Btn>
                  <Btn onClick={() => void downloadExport(selectedId, 'combined')}>Combined</Btn>
                </div>
                <div className="mt-3 min-h-64 rounded-md border border-border p-4">
                  {agentTab === 'notes' ? (
                    <MarkdownRenderer
                      content={agents.data?.outputs[NOTE_AGENT] ?? '_Notes pending._'}
                    />
                  ) : (
                    <div>
                      <p className="text-xs text-muted mb-3">
                        Sketch output is isolated with scripts, navigation, and network access disabled.
                      </p>
                      <iframe
                        title="Meeting sketch"
                        sandbox="allow-scripts"
                        srcDoc={buildSketchSrcdoc(
                          agents.data?.outputs[SKETCH_AGENT] ?? '<p>Sketch pending.</p>',
                        )}
                        className="h-[30rem] w-full rounded-md border border-border bg-surface"
                      />
                    </div>
                  )}
                </div>
              </section>

              <section className="min-w-0 lg:border-l lg:border-border lg:pl-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="font-medium text-text">Transcript</h3>
                  <div className="flex flex-wrap gap-2">
                    <Btn onClick={() => void downloadExport(selectedId, 'markdown')}>
                      Markdown
                    </Btn>
                    <Btn onClick={() => void downloadExport(selectedId, 'text')}>
                      Text
                    </Btn>
                    <Btn onClick={() => void downloadExport(selectedId, 'json')}>
                      JSON
                    </Btn>
                  </div>
                </div>
                <div className="mt-3 max-h-[32rem] space-y-2 overflow-y-auto pr-2">
                  {visibleTranscriptRows(transcript.data?.segments ?? []).map(row => (
                    row.kind === 'speech' ? (
                      <div key={row.key}>
                        {row.timestamp ? (
                          <div className="mb-1 mt-4 text-xs font-medium tabular-nums text-muted">
                            {row.timestamp}
                          </div>
                        ) : null}
                        <p className="text-sm leading-relaxed text-text">{row.text}</p>
                      </div>
                    ) : (
                      <div
                        key={row.key}
                        className="flex items-center justify-between gap-3 rounded-md bg-muted/20 px-3 py-2"
                      >
                        <span className="text-xs text-muted">
                          Silence covered
                          {' · '}
                          {(row.segment.audio_start_sample / 16000).toFixed(1)}–
                          {(row.segment.audio_end_sample / 16000).toFixed(1)}s
                        </span>
                        <Btn
                          onClick={() => void retryCoverage(row.segment.id)}
                          disabled={retryingSegmentId !== undefined}
                        >
                          {retryingSegmentId === row.segment.id
                            ? 'Retrying…'
                            : 'Recognize again'}
                        </Btn>
                      </div>
                    )
                  ))}
                  {transcript.data && transcript.data.segments.length === 0 ? (
                    <p className="text-sm text-muted">No committed speech yet.</p>
                  ) : null}
                </div>
              </section>
            </div>

            <section className="mt-5 border-t border-border pt-4">
              <h3 className="font-medium text-text">Meeting Chat</h3>
              <p className="mt-1 text-xs text-muted">
                Add participants, agenda, terminology, or corrections. Messages go to both agents
                and never rewrite the raw transcript.
              </p>
              {(agents.data?.context ?? []).length > 0 ? (
                <div className="mt-3 max-h-32 space-y-2 overflow-y-auto">
                  {agents.data?.context.map(event => (
                    <div key={event.id} className="rounded-md bg-muted/20 px-3 py-2 text-sm">
                      <span className="mr-2 text-xs text-muted">Typed · {fmtDateTime(event.created_at)}</span>
                      {event.text}
                    </div>
                  ))}
                </div>
              ) : null}
              <div className="mt-3 flex flex-col gap-2 md:flex-row">
                <Input
                  value={meetingContext}
                  onChange={event => setMeetingContext(event.target.value)}
                  placeholder="Tell the meeting participants, agenda, or a correction…"
                  aria-label="Meeting context"
                  maxLength={4000}
                  disabled={detail.data?.meeting.state !== 'recording'}
                />
                <Btn
                  onClick={() => sendMeetingContext.mutate(meetingContext.trim())}
                  disabled={
                    detail.data?.meeting.state !== 'recording'
                    || !meetingContext.trim()
                    || sendMeetingContext.isPending
                  }
                >
                  {sendMeetingContext.isPending ? 'Sending…' : 'Send'}
                </Btn>
              </div>
              {detail.data?.meeting.state !== 'recording' ? (
                <p className="mt-2 text-xs text-muted">
                  Meeting Chat is available while recording.
                </p>
              ) : null}
            </section>
          </Card>
        ) : null}
      </div>
    </>
  )
}
