import type { AppApi } from '@kirocrew/app-sdk'
import type { CaptureQueue, QueuedChunk } from './captureQueue'

const BACKEND_API = '/apps/whisper-meeting/api'

type ChunkAck = {
  meeting_id: string
  epoch: number
  sequence: number
  sha256: string
  durable: boolean
  duplicate: boolean
}

export type UploadState = {
  running: boolean
  lastAckSequence?: number
  error?: string
}

export class DurableUploader {
  private running = false
  private rerun = false
  private activeDrain?: Promise<void>
  private state: UploadState = { running: false }
  private listeners = new Set<(state: UploadState) => void>()

  constructor(
    private readonly api: AppApi,
    private readonly queue: CaptureQueue,
  ) {}

  subscribe(listener: (state: UploadState) => void): () => void {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  async drain(): Promise<void> {
    if (this.activeDrain) {
      this.rerun = true
      await this.activeDrain
      return
    }
    const active = this.runDrain()
    this.activeDrain = active
    try {
      await active
    } finally {
      if (this.activeDrain === active) this.activeDrain = undefined
    }
  }

  private async runDrain(): Promise<void> {
    this.running = true
    this.publish({ ...this.state, running: true, error: undefined })
    try {
      do {
        this.rerun = false
        let chunk = await this.queue.first()
        while (chunk) {
          const ack = await this.upload(chunk)
          this.assertAck(chunk, ack)
          await this.queue.deleteExact(chunk)
          this.publish({ running: true, lastAckSequence: chunk.sequence })
          chunk = await this.queue.first()
        }
      } while (this.rerun)
      this.publish({ ...this.state, running: false, error: undefined })
    } catch (error) {
      this.publish({
        ...this.state,
        running: false,
        error: error instanceof Error ? error.message : String(error),
      })
      throw error
    } finally {
      this.running = false
    }
  }

  private async upload(chunk: QueuedChunk): Promise<ChunkAck> {
    const target = `${BACKEND_API}/meetings/${encodeURIComponent(chunk.meetingId)}`
      + `/audio/${chunk.epoch}/${chunk.sequence}`
    const response = await this.api.request<{ ack: ChunkAck }>(target, {
      method: 'PUT',
      body: chunk.pcm,
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Chunk-SHA256': chunk.sha256,
        'X-Sample-Start': String(chunk.sampleStart),
        'X-Sample-Count': String(chunk.sampleCount),
        'X-Sample-Rate': String(chunk.sampleRate),
        'X-Channels': String(chunk.channels),
        'X-Encoding': chunk.encoding,
      },
    })
    return response.ack
  }

  private assertAck(chunk: QueuedChunk, ack: ChunkAck): void {
    if (
      !ack.durable
      || ack.meeting_id !== chunk.meetingId
      || ack.epoch !== chunk.epoch
      || ack.sequence !== chunk.sequence
      || ack.sha256 !== chunk.sha256
    ) {
      throw new Error('backend returned a mismatched durable ACK')
    }
  }

  private publish(state: UploadState): void {
    this.state = state
    for (const listener of this.listeners) listener(state)
  }
}
