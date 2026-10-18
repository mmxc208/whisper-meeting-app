import { IDBFactory } from 'fake-indexeddb'
import { describe, expect, it } from 'vitest'
import { BrowserQueueLimitError, CaptureQueue, chunkKey, type QueuedChunk } from '../src/captureQueue'

function chunk(sequence: number, bytes = 16): QueuedChunk {
  return {
    key: chunkKey('meeting-a', 1, sequence),
    meetingId: 'meeting-a',
    epoch: 1,
    sequence,
    capturedAt: '2026-10-17T12:00:00.000Z',
    sampleStart: sequence * (bytes / 2),
    sampleCount: bytes / 2,
    sampleRate: 16000,
    channels: 1,
    encoding: 'pcm_s16le',
    sha256: String(sequence).padStart(64, '0'),
    pcm: new ArrayBuffer(bytes),
  }
}

describe('CaptureQueue', () => {
  it('survives a new queue instance and preserves upload order', async () => {
    const factory = new IDBFactory()
    const first = new CaptureQueue(factory)
    await first.put(chunk(1))
    await first.put(chunk(0))
    first.close()

    const reopened = new CaptureQueue(factory)
    expect((await reopened.first())?.sequence).toBe(0)
    expect(await reopened.stats()).toEqual({ chunks: 2, bytes: 32 })
    reopened.close()
  })

  it('accepts an exact duplicate without double-counting bytes', async () => {
    const queue = new CaptureQueue(new IDBFactory())
    const value = chunk(0)
    await queue.put(value)
    await queue.put(value)

    expect(await queue.stats()).toEqual({ chunks: 1, bytes: 16 })
    queue.close()
  })

  it('keeps the existing chunk when the browser cap is reached', async () => {
    const queue = new CaptureQueue(new IDBFactory(), 16)
    await queue.put(chunk(0))

    await expect(queue.put(chunk(1))).rejects.toBeInstanceOf(BrowserQueueLimitError)
    expect(await queue.stats()).toEqual({ chunks: 1, bytes: 16 })
    expect((await queue.first())?.sequence).toBe(0)
    queue.close()
  })

  it('refuses a sequence conflict', async () => {
    const queue = new CaptureQueue(new IDBFactory())
    const original = chunk(0)
    await queue.put(original)

    await expect(queue.put({ ...original, sha256: 'f'.repeat(64) })).rejects.toThrow(
      'sequence conflict',
    )
    expect((await queue.first())?.sha256).toBe(original.sha256)
    queue.close()
  })
})
