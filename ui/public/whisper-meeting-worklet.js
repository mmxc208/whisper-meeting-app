const TARGET_RATE = 16000
const CHUNK_SAMPLES = TARGET_RATE / 2
const FILTER_TAPS = 63
const FILTER_PHASES = 256

class WhisperMeetingPcmWorklet extends AudioWorkletProcessor {
  constructor () {
    super()
    this._phase = 0
    this._history = new Float32Array(FILTER_TAPS)
    this._position = 0
    this._phases = sampleRate % TARGET_RATE === 0 ? 1 : FILTER_PHASES
    this._filter = new Float32Array(FILTER_TAPS * this._phases)
    const cutoff = Math.min(0.5, TARGET_RATE / sampleRate * 0.45)
    const middle = (FILTER_TAPS - 1) / 2
    for (let phase = 0; phase < this._phases; phase++) {
      let sum = 0
      const offset = phase * FILTER_TAPS
      for (let index = 0; index < FILTER_TAPS; index++) {
        const x = index - middle - phase / this._phases
        const sinc = x === 0
          ? 2 * cutoff
          : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x)
        const window = 0.54 - 0.46 * Math.cos(2 * Math.PI * index / (FILTER_TAPS - 1))
        sum += this._filter[offset + index] = sinc * window
      }
      for (let index = 0; index < FILTER_TAPS; index++) {
        this._filter[offset + index] /= sum
      }
    }
    this._chunk = new Int16Array(CHUNK_SAMPLES)
    this._chunkLength = 0
    this._stopped = false
    this._hasInput = false
    this.port.onmessage = event => {
      if (event.data?.type !== 'flush' || this._stopped) return
      if (this._hasInput && sampleRate > TARGET_RATE) {
        this.process([[new Float32Array(FILTER_TAPS - 1)]])
      }
      this._stopped = true
      this._flush()
      this.port.postMessage({ type: 'flushed' })
    }
  }

  _flush () {
    if (!this._chunkLength) return
    const output = this._chunk.slice(0, this._chunkLength)
    this.port.postMessage(output.buffer, [output.buffer])
    this._chunkLength = 0
  }

  process (inputs) {
    if (this._stopped) return false
    const channel = inputs[0]?.[0]
    if (!channel?.length) return true
    this._hasInput = true
    for (let inputIndex = 0; inputIndex < channel.length; inputIndex++) {
      this._history[this._position] = channel[inputIndex]
      this._phase += TARGET_RATE
      while (this._phase >= sampleRate) {
        this._phase -= sampleRate
        let sample = channel[inputIndex]
        if (sampleRate > TARGET_RATE) {
          sample = 0
          const phaseOffset = Math.floor(this._phase / TARGET_RATE * this._phases) * FILTER_TAPS
          for (let tap = 0; tap < FILTER_TAPS; tap++) {
            sample += this._history[(this._position - tap + FILTER_TAPS) % FILTER_TAPS] * this._filter[phaseOffset + tap]
          }
        }
        sample = Math.max(-1, Math.min(1, sample))
        this._chunk[this._chunkLength++] = sample < 0 ? sample * 0x8000 : sample * 0x7FFF
        if (this._chunkLength === CHUNK_SAMPLES) this._flush()
      }
      this._position = (this._position + 1) % FILTER_TAPS
    }
    return true
  }
}

registerProcessor('whisper-meeting-pcm', WhisperMeetingPcmWorklet)

/**
 * Diagnostic probe: reports how many channels the browser actually delivers
 * for an input and the level on each, so a misrouted aggregate device (mic in
 * channel 1, the call in channels 2-3) is visible before a meeting starts.
 * It never touches the PCM path above.
 */
class WhisperMeetingChannelProbe extends AudioWorkletProcessor {
  constructor () {
    super()
    this._sumSquares = []
    this._peaks = []
    this._frames = 0
    this._reportEvery = Math.max(128, Math.floor(sampleRate / 4))
    this._stopped = false
    this.port.onmessage = event => {
      if (event.data?.type === 'stop') this._stopped = true
    }
  }

  process (inputs) {
    if (this._stopped) return false
    const channels = inputs[0] ?? []
    if (!channels.length || !channels[0].length) return true
    while (this._sumSquares.length < channels.length) {
      this._sumSquares.push(0)
      this._peaks.push(0)
    }
    for (let channel = 0; channel < channels.length; channel++) {
      const samples = channels[channel]
      let sum = this._sumSquares[channel]
      let peak = this._peaks[channel]
      for (let index = 0; index < samples.length; index++) {
        const value = samples[index]
        sum += value * value
        const magnitude = value < 0 ? -value : value
        if (magnitude > peak) peak = magnitude
      }
      this._sumSquares[channel] = sum
      this._peaks[channel] = peak
    }
    this._frames += channels[0].length
    if (this._frames >= this._reportEvery) {
      this.port.postMessage({
        type: 'levels',
        channels: channels.length,
        frames: this._frames,
        sumSquares: this._sumSquares.slice(),
        peaks: this._peaks.slice(),
      })
      this._sumSquares = this._sumSquares.map(() => 0)
      this._peaks = this._peaks.map(() => 0)
      this._frames = 0
    }
    return true
  }
}

registerProcessor('whisper-meeting-channel-probe', WhisperMeetingChannelProbe)
