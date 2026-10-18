import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { describe, expect, it } from 'vitest'

const source = readFileSync('public/whisper-meeting-worklet.js', 'utf8')

function capture(rate: number, duration: number) {
  const messages: unknown[] = []
  let Processor!: new () => {
    process(inputs: Float32Array[][]): boolean
    port: { onmessage: (event: unknown) => void }
  }
  runInNewContext(source, {
    sampleRate: rate,
    Float32Array,
    Int16Array,
    Math,
    AudioWorkletProcessor: class {
      port = { postMessage: (data: unknown) => messages.push(data) }
    },
    registerProcessor: (_name: string, processor: typeof Processor) => { Processor = processor },
  })
  const worklet = new Processor()
  const sampleTotal = Math.round(rate * duration)
  for (let start = 0; start < sampleTotal; start += 128) {
    const input = Float32Array.from(
      { length: Math.min(128, sampleTotal - start) },
      (_, index) => 0.5 * Math.sin(2 * Math.PI * 1000 * (start + index) / rate),
    )
    worklet.process([[input]])
  }
  worklet.port.onmessage({ data: { type: 'flush' } })
  return messages
}

describe('Whisper Meeting worklet', () => {
  it.each([16000, 44100, 48000])('emits independent 500 ms PCM chunks at %i Hz', rate => {
    const messages = capture(rate, 1.1)
    const chunks = messages.slice(0, -1).map(value => new Int16Array(value as ArrayBuffer))

    expect(chunks.slice(0, 2).map(value => value.length)).toEqual([8000, 8000])
    expect(chunks[2].length).toBeGreaterThan(0)
    expect(chunks[2].length).toBeLessThan(8000)
    expect(messages.at(-1)).toEqual({ type: 'flushed' })
  })
})
