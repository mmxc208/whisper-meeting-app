import type { AppApi } from '@kirocrew/app-sdk'
import { CaptureQueue, chunkKey, type QueuedChunk } from './captureQueue'
import { DurableUploader, type UploadState } from './uploader'

const BACKEND_API = '/apps/whisper-meeting/api'
const WORKLET_URL = '/apps/whisper-meeting/ui/dist/whisper-meeting-worklet.js'

type CapturePhase =
  | 'idle'
  | 'starting'
  | 'recording'
  | 'stopping'
  | 'uploading'
  | 'transcribing'
  | 'paused'
  | 'ended'
  | 'recovering'
  | 'error'

export type CaptureState = {
  phase: CapturePhase
  meetingId?: string
  epoch?: number
  queuedChunks: number
  queuedBytes: number
  lastAckSequence?: number
  uploadError?: string
  fatalError?: string
}

type StartResult = {
  capture: { meeting_id: string; epoch: number; state: string }
}

type DrainKind = 'pause' | 'end'

type DrainResult = {
  operation: { state: string }
}

type MeetingProgressResult = {
  meeting: { state: string }
  capture: {
    epoch: number
    cut_sequence: number
    cut_sample: number
  } | null
}

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}

class CaptureEngine {
  private readonly queue = new CaptureQueue()
  private readonly listeners = new Set<(state: CaptureState) => void>()
  private state: CaptureState = { phase: 'idle', queuedChunks: 0, queuedBytes: 0 }
  private api?: AppApi
  private uploader?: DurableUploader
  private unsubscribeUploader?: () => void
  private stream?: MediaStream
  private context?: AudioContext
  private source?: MediaStreamAudioSourceNode
  private worklet?: AudioWorkletNode
  private meetingId?: string
  private epoch?: number
  private sequence = 0
  private sampleStart = 0
  private persistChain: Promise<void> = Promise.resolve()
  private flushComplete?: () => void

  subscribe(listener: (state: CaptureState) => void): () => void {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  snapshot(): CaptureState {
    return this.state
  }

  async attach(api: AppApi): Promise<void> {
    if (this.state.phase !== 'recording') this.ensureUploader(api)
    await this.refreshQueueStats()
    try {
      await this.uploader?.drain()
    } catch {
      // The durable queue remains intact. A later attach or captured chunk retries it.
    }
  }

  async start(api: AppApi, meetingId: string, resume = false): Promise<void> {
    if (!['idle', 'paused', 'recovering', 'error', 'ended'].includes(this.state.phase)) {
      throw new Error('capture is already active')
    }
    this.ensureUploader(api)
    this.publish({ ...this.state, phase: 'starting', fatalError: undefined })
    await this.attach(api)
    const queued = await this.queue.stats()
    if (queued.chunks > 0) {
      this.publish({
        ...this.state,
        phase: 'error',
        fatalError: 'Queued audio must upload before a new capture epoch can start.',
      })
      throw new Error('queued audio has not drained')
    }

    let stream: MediaStream | undefined
    let context: AudioContext | undefined
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false },
      })
      context = new AudioContext()
      await context.audioWorklet.addModule(WORKLET_URL)
      const source = context.createMediaStreamSource(stream)
      const worklet = new AudioWorkletNode(context, 'whisper-meeting-pcm')
      const response = await api.post<StartResult>(
        `${BACKEND_API}/meetings/${encodeURIComponent(meetingId)}/${resume ? 'resume' : 'start'}`,
        {},
      )

      this.stream = stream
      this.context = context
      this.source = source
      this.worklet = worklet
      this.meetingId = meetingId
      this.epoch = response.capture.epoch
      this.sequence = 0
      this.sampleStart = 0
      this.persistChain = Promise.resolve()
      worklet.port.onmessage = event => this.onWorkletMessage(event)
      source.connect(worklet)
      this.publish({
        ...this.state,
        phase: 'recording',
        meetingId,
        epoch: response.capture.epoch,
        fatalError: undefined,
      })
    } catch (error) {
      stream?.getTracks().forEach(track => track.stop())
      if (context) await context.close().catch(() => undefined)
      this.publish({
        ...this.state,
        phase: 'error',
        fatalError: error instanceof Error ? error.message : String(error),
      })
      throw error
    }
  }

  pause(api: AppApi): Promise<void> {
    return this.finishLocalCapture(api, 'pause')
  }

  end(api: AppApi): Promise<void> {
    return this.finishLocalCapture(api, 'end')
  }

  async recover(api: AppApi, meetingId: string, kind: DrainKind): Promise<void> {
    if (this.state.phase === 'recording') throw new Error('local capture is still active')
    this.ensureUploader(api)
    this.publish({ ...this.state, phase: 'uploading', meetingId, fatalError: undefined })
    await this.uploader?.drain()
    const stats = await this.queue.stats()
    if (stats.chunks !== 0) throw new Error('queued audio has not drained')
    const progress = await api.get<MeetingProgressResult>(
      `${BACKEND_API}/meetings/${encodeURIComponent(meetingId)}`,
    )
    if (!progress.capture) throw new Error('meeting has no capture epoch to drain')
    await this.requestDrain(api, meetingId, kind, progress.capture)
  }

  markServerState(meetingId: string, state: string): void {
    if (this.state.meetingId !== meetingId) return
    if (state === 'paused' || state === 'ended') {
      this.publish({ ...this.state, phase: state, uploadError: undefined })
    }
  }

  private async finishLocalCapture(api: AppApi, kind: DrainKind): Promise<void> {
    if (this.state.phase !== 'recording' || !this.worklet) return
    this.publish({ ...this.state, phase: 'stopping' })
    const flushed = new Promise<void>(resolve => { this.flushComplete = resolve })
    this.worklet.port.postMessage({ type: 'flush' })
    await flushed
    await this.persistChain
    this.source?.disconnect()
    this.worklet.disconnect()
    this.stream?.getTracks().forEach(track => track.stop())
    await this.context?.close().catch(() => undefined)
    this.clearHardware()
    this.publish({ ...this.state, phase: 'uploading' })
    try {
      await this.uploader?.drain()
      const stats = await this.queue.stats()
      if (stats.chunks !== 0) throw new Error('queued audio has not drained')
      if (!this.meetingId || this.epoch === undefined) {
        throw new Error('capture identity is missing')
      }
      await this.requestDrain(api, this.meetingId, kind, {
        epoch: this.epoch,
        cut_sequence: this.sequence - 1,
        cut_sample: this.sampleStart,
      })
    } catch (error) {
      this.publish({
        ...this.state,
        phase: 'recovering',
        fatalError: error instanceof Error ? error.message : String(error),
      })
      throw error
    }
  }

  private async requestDrain(
    api: AppApi,
    meetingId: string,
    kind: DrainKind,
    cut: { epoch: number; cut_sequence: number; cut_sample: number },
  ): Promise<void> {
    this.publish({ ...this.state, phase: 'transcribing', meetingId, epoch: cut.epoch })
    const response = await api.post<DrainResult>(
      `${BACKEND_API}/meetings/${encodeURIComponent(meetingId)}/${kind}`,
      cut,
    )
    if (response.operation.state === 'complete') {
      this.publish({ ...this.state, phase: kind === 'pause' ? 'paused' : 'ended' })
    }
  }

  private configureUploader(api: AppApi): void {
    this.api = api
    this.unsubscribeUploader?.()
    this.uploader = new DurableUploader(api, this.queue)
    this.unsubscribeUploader = this.uploader.subscribe(state => this.onUploadState(state))
  }

  private ensureUploader(api: AppApi): void {
    if (this.api !== api || !this.uploader) this.configureUploader(api)
  }

  private onWorkletMessage(event: MessageEvent<ArrayBuffer | { type: string }>): void {
    if (event.data instanceof ArrayBuffer) {
      const pcm = event.data
      this.persistChain = this.persistChain.then(() => this.persist(pcm)).catch(error => {
        this.publish({
          ...this.state,
          phase: 'error',
          fatalError: error instanceof Error ? error.message : String(error),
        })
        void this.stopHardwareAfterStorageFailure()
      })
      return
    }
    if (event.data?.type === 'flushed') this.flushComplete?.()
  }

  private async persist(pcm: ArrayBuffer): Promise<void> {
    if (!this.meetingId || this.epoch === undefined) throw new Error('capture identity is missing')
    const sampleCount = pcm.byteLength / 2
    const chunk: QueuedChunk = {
      key: chunkKey(this.meetingId, this.epoch, this.sequence),
      meetingId: this.meetingId,
      epoch: this.epoch,
      sequence: this.sequence,
      capturedAt: new Date().toISOString(),
      sampleStart: this.sampleStart,
      sampleCount,
      sampleRate: 16000,
      channels: 1,
      encoding: 'pcm_s16le',
      sha256: await sha256Hex(pcm),
      pcm,
    }
    await this.queue.put(chunk)
    this.sequence += 1
    this.sampleStart += sampleCount
    await this.refreshQueueStats()
    void this.uploader?.drain().catch(() => undefined)
  }

  private async stopHardwareAfterStorageFailure(): Promise<void> {
    this.source?.disconnect()
    this.worklet?.disconnect()
    this.stream?.getTracks().forEach(track => track.stop())
    await this.context?.close().catch(() => undefined)
    this.clearHardware()
  }

  private clearHardware(): void {
    this.stream = undefined
    this.context = undefined
    this.source = undefined
    this.worklet = undefined
    this.flushComplete = undefined
  }

  private onUploadState(upload: UploadState): void {
    void this.refreshQueueStats()
    this.publish({
      ...this.state,
      lastAckSequence: upload.lastAckSequence ?? this.state.lastAckSequence,
      uploadError: upload.error,
    })
  }

  private async refreshQueueStats(): Promise<void> {
    const stats = await this.queue.stats()
    this.publish({ ...this.state, queuedChunks: stats.chunks, queuedBytes: stats.bytes })
  }

  private publish(state: CaptureState): void {
    this.state = state
    for (const listener of this.listeners) listener(state)
  }
}

export const captureEngine = new CaptureEngine()
export const subscribeCapture = (listener: () => void) => captureEngine.subscribe(listener)
export const captureSnapshot = () => captureEngine.snapshot()
