export const BROWSER_QUEUE_CAP_BYTES = 512 * 1024 * 1024
const DATABASE_NAME = 'whisper-meeting-capture-v1'
const DATABASE_VERSION = 1
const CHUNKS = 'chunks'
const META = 'meta'
const BY_ORDER = 'by-order'
const BYTE_TOTAL = 'byte-total'

export type QueuedChunk = {
  key: string
  meetingId: string
  epoch: number
  sequence: number
  capturedAt: string
  sampleStart: number
  sampleCount: number
  sampleRate: 16000
  channels: 1
  encoding: 'pcm_s16le'
  sha256: string
  pcm: ArrayBuffer
}

export class BrowserQueueLimitError extends Error {}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'))
  })
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onabort = () => reject(transaction.error ?? new Error('IndexedDB transaction aborted'))
    transaction.onerror = () => reject(transaction.error ?? new Error('IndexedDB transaction failed'))
  })
}

export function chunkKey(meetingId: string, epoch: number, sequence: number): string {
  return `${meetingId}:${String(epoch).padStart(8, '0')}:${String(sequence).padStart(16, '0')}`
}

export class CaptureQueue {
  private databasePromise?: Promise<IDBDatabase>

  constructor(
    private readonly factory: IDBFactory = indexedDB,
    private readonly capBytes = BROWSER_QUEUE_CAP_BYTES,
  ) {}

  async put(chunk: QueuedChunk): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction([CHUNKS, META], 'readwrite')
    const done = transactionDone(transaction)
    const chunks = transaction.objectStore(CHUNKS)
    const meta = transaction.objectStore(META)
    const existing = await requestResult(chunks.get(chunk.key)) as QueuedChunk | undefined
    if (existing) {
      if (existing.sha256 !== chunk.sha256 || existing.pcm.byteLength !== chunk.pcm.byteLength) {
        transaction.abort()
        await done.catch(() => undefined)
        throw new Error('browser queue sequence conflict')
      }
      await done
      return
    }
    const total = Number((await requestResult(meta.get(BYTE_TOTAL))) ?? 0)
    if (total + chunk.pcm.byteLength > this.capBytes) {
      transaction.abort()
      await done.catch(() => undefined)
      throw new BrowserQueueLimitError('browser audio queue limit reached')
    }
    chunks.put(chunk)
    meta.put(total + chunk.pcm.byteLength, BYTE_TOTAL)
    await done
  }

  async first(): Promise<QueuedChunk | undefined> {
    const database = await this.database()
    const transaction = database.transaction(CHUNKS, 'readonly')
    const done = transactionDone(transaction)
    const request = transaction.objectStore(CHUNKS).index(BY_ORDER).openCursor()
    const chunk = await new Promise<QueuedChunk | undefined>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result?.value as QueuedChunk | undefined)
      request.onerror = () => reject(request.error ?? new Error('IndexedDB cursor failed'))
    })
    await done
    return chunk
  }

  async deleteExact(chunk: QueuedChunk): Promise<void> {
    const database = await this.database()
    const transaction = database.transaction([CHUNKS, META], 'readwrite')
    const done = transactionDone(transaction)
    const chunks = transaction.objectStore(CHUNKS)
    const meta = transaction.objectStore(META)
    const existing = await requestResult(chunks.get(chunk.key)) as QueuedChunk | undefined
    if (existing?.sha256 === chunk.sha256) {
      const total = Number((await requestResult(meta.get(BYTE_TOTAL))) ?? 0)
      chunks.delete(chunk.key)
      meta.put(Math.max(0, total - existing.pcm.byteLength), BYTE_TOTAL)
    }
    await done
  }

  async stats(): Promise<{ chunks: number; bytes: number }> {
    const database = await this.database()
    const transaction = database.transaction([CHUNKS, META], 'readonly')
    const done = transactionDone(transaction)
    const chunks = await requestResult(transaction.objectStore(CHUNKS).count())
    const bytes = Number((await requestResult(transaction.objectStore(META).get(BYTE_TOTAL))) ?? 0)
    await done
    return { chunks, bytes }
  }

  close(): void {
    if (!this.databasePromise) return
    void this.databasePromise.then(database => database.close())
    this.databasePromise = undefined
  }

  private database(): Promise<IDBDatabase> {
    if (!this.databasePromise) {
      this.databasePromise = new Promise((resolve, reject) => {
        const request = this.factory.open(DATABASE_NAME, DATABASE_VERSION)
        request.onupgradeneeded = () => {
          const database = request.result
          const chunks = database.createObjectStore(CHUNKS, { keyPath: 'key' })
          chunks.createIndex(BY_ORDER, 'key', { unique: true })
          database.createObjectStore(META)
        }
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error ?? new Error('IndexedDB open failed'))
      })
    }
    return this.databasePromise
  }
}
