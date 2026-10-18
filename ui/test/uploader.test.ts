import { IDBFactory } from 'fake-indexeddb'
import { describe, expect, it, vi } from 'vitest'
import type { AppApi } from '@kirocrew/app-sdk'
import { CaptureQueue, chunkKey, type QueuedChunk } from '../src/captureQueue'
import { DurableUploader } from '../src/uploader'

function chunk(): QueuedChunk {
  return {
    key: chunkKey('meeting-a', 1, 0),
    meetingId: 'meeting-a',
    epoch: 1,
    sequence: 0,
    capturedAt: '2026-10-17T12:00:00.000Z',
    sampleStart: 0,
    sampleCount: 8,
    sampleRate: 16000,
    channels: 1,
    encoding: 'pcm_s16le',
    sha256: 'a'.repeat(64),
    pcm: new ArrayBuffer(16),
  }
}

function api(request: AppApi['request']): AppApi {
  return {
    request,
    get: vi.fn(),
    post: vi.fn(),
  }
}

function ack(value: QueuedChunk) {
  return {
    ack: {
      meeting_id: value.meetingId,
      epoch: value.epoch,
      sequence: value.sequence,
      sha256: value.sha256,
      durable: true,
      duplicate: false,
    },
  }
}

describe('DurableUploader', () => {
  it('deletes PCM only after an exact durable ACK', async () => {
    const queue = new CaptureQueue(new IDBFactory())
    const value = chunk()
    await queue.put(value)
    const request = vi.fn().mockResolvedValue(ack(value))

    await new DurableUploader(api(request), queue).drain()

    expect(await queue.stats()).toEqual({ chunks: 0, bytes: 0 })
    expect(request).toHaveBeenCalledWith(
      '/apps/whisper-meeting/api/meetings/meeting-a/audio/1/0',
      expect.objectContaining({ method: 'PUT', body: value.pcm }),
    )
    queue.close()
  })

  it('retains PCM after a network failure and retries it later', async () => {
    const queue = new CaptureQueue(new IDBFactory())
    const value = chunk()
    await queue.put(value)
    const request = vi.fn()
      .mockRejectedValueOnce(new Error('connection reset'))
      .mockResolvedValueOnce(ack(value))
    const uploader = new DurableUploader(api(request), queue)

    await expect(uploader.drain()).rejects.toThrow('connection reset')
    expect(await queue.stats()).toEqual({ chunks: 1, bytes: 16 })
    await uploader.drain()

    expect(request).toHaveBeenCalledTimes(2)
    expect(await queue.stats()).toEqual({ chunks: 0, bytes: 0 })
    queue.close()
  })

  it('retains PCM when the backend ACK does not identify the chunk', async () => {
    const queue = new CaptureQueue(new IDBFactory())
    const value = chunk()
    await queue.put(value)
    const response = ack(value)
    response.ack.sequence = 9

    await expect(new DurableUploader(api(vi.fn().mockResolvedValue(response)), queue).drain())
      .rejects.toThrow('mismatched durable ACK')

    expect(await queue.stats()).toEqual({ chunks: 1, bytes: 16 })
    queue.close()
  })

  it('makes concurrent drain callers wait for the active durable ACK', async () => {
    const queue = new CaptureQueue(new IDBFactory())
    const value = chunk()
    await queue.put(value)
    let release: (() => void) | undefined
    let markStarted: (() => void) | undefined
    const started = new Promise<void>(resolve => { markStarted = resolve })
    const request = vi.fn().mockImplementation(() => new Promise(resolve => {
      markStarted?.()
      release = () => resolve(ack(value))
    }))
    const uploader = new DurableUploader(api(request), queue)

    const first = uploader.drain()
    await started
    expect(request).toHaveBeenCalledTimes(1)
    let secondFinished = false
    const second = uploader.drain().then(() => { secondFinished = true })
    await Promise.resolve()

    expect(secondFinished).toBe(false)
    release?.()
    await Promise.all([first, second])
    expect(await queue.stats()).toEqual({ chunks: 0, bytes: 0 })
    queue.close()
  })
})
