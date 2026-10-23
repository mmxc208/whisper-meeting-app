/**
 * Input device probe. Opens the microphone the way the app requests it today
 * (one channel, processing off) and once more asking for every channel, and
 * reports what the browser actually delivered: the track settings, the channel
 * count seen inside an AudioWorklet, and the level on each channel.
 *
 * Whisper Meeting records whatever reaches the selected input. On an aggregate
 * device the microphone is channel 1 and the call lands in channels 2 and 3,
 * so a one-channel request silently drops the call. This probe makes that
 * visible before a meeting starts and verifies the mixing fix afterwards.
 */

export type LevelMessage = {
  type: 'levels'
  channels: number
  frames: number
  sumSquares: number[]
  peaks: number[]
}

export type ChannelLevels = {
  channels: number
  frames: number
  rmsDb: number[]
  peakDb: number[]
}

export type ProbePass = {
  label: string
  requestedChannels: string
  trackLabel: string
  settings: {
    channelCount?: number
    sampleRate?: number
    echoCancellation?: boolean
    noiseSuppression?: boolean
    autoGainControl?: boolean
  }
  levels: ChannelLevels
}

export type ProbeReport = {
  inputs: string[]
  passes: ProbePass[]
}

const SILENCE_DB = -120

export function toDb(amplitude: number): number {
  if (!(amplitude > 0)) return SILENCE_DB
  return Math.max(SILENCE_DB, Math.round(20 * Math.log10(amplitude) * 10) / 10)
}

/** Fold the worklet's windowed reports into one level per channel. */
export function summarizeLevels(messages: LevelMessage[]): ChannelLevels {
  const channels = messages.reduce((most, message) => Math.max(most, message.channels), 0)
  const sumSquares = new Array<number>(channels).fill(0)
  const peaks = new Array<number>(channels).fill(0)
  let frames = 0
  for (const message of messages) {
    frames += message.frames
    for (let channel = 0; channel < message.channels; channel++) {
      sumSquares[channel] += message.sumSquares[channel] ?? 0
      peaks[channel] = Math.max(peaks[channel], message.peaks[channel] ?? 0)
    }
  }
  return {
    channels,
    frames,
    rmsDb: sumSquares.map(sum => toDb(frames > 0 ? Math.sqrt(sum / frames) : 0)),
    peakDb: peaks.map(toDb),
  }
}

export function describeLevel(db: number): string {
  if (db <= SILENCE_DB) return 'silent'
  if (db < -60) return 'silent (noise floor)'
  if (db < -40) return 'quiet'
  return 'active'
}

type PassSpec = { label: string; requestedChannels: string; audio: MediaTrackConstraints }

const PASSES: PassSpec[] = [
  {
    label: 'As the app records today',
    requestedChannels: '1',
    audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false },
  },
  {
    label: 'Asking for every channel',
    requestedChannels: 'all',
    audio: {
      channelCount: { ideal: 32 },
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  },
]

async function runPass(spec: PassSpec, workletUrl: string, durationMs: number): Promise<ProbePass> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: spec.audio })
  const context = new AudioContext()
  const messages: LevelMessage[] = []
  try {
    await context.audioWorklet.addModule(workletUrl)
    const source = context.createMediaStreamSource(stream)
    const probe = new AudioWorkletNode(context, 'whisper-meeting-channel-probe')
    probe.port.onmessage = (event: MessageEvent<LevelMessage>) => {
      if (event.data?.type === 'levels') messages.push(event.data)
    }
    source.connect(probe)
    await new Promise(resolve => setTimeout(resolve, durationMs))
    probe.port.postMessage({ type: 'stop' })
    source.disconnect()
    probe.disconnect()
    const track = stream.getAudioTracks()[0]
    const settings = track?.getSettings() ?? {}
    return {
      label: spec.label,
      requestedChannels: spec.requestedChannels,
      trackLabel: track?.label ?? '',
      settings: {
        channelCount: settings.channelCount,
        sampleRate: settings.sampleRate,
        echoCancellation: settings.echoCancellation,
        noiseSuppression: settings.noiseSuppression,
        autoGainControl: settings.autoGainControl,
      },
      levels: summarizeLevels(messages),
    }
  } finally {
    stream.getTracks().forEach(track => track.stop())
    await context.close().catch(() => undefined)
  }
}

export async function probeInput(workletUrl: string, durationMs = 2500): Promise<ProbeReport> {
  const passes: ProbePass[] = []
  for (const spec of PASSES) passes.push(await runPass(spec, workletUrl, durationMs))
  const devices = await navigator.mediaDevices.enumerateDevices().catch(() => [])
  const inputs = devices
    .filter(device => device.kind === 'audioinput')
    .map(device => device.label || '(unnamed input)')
  return { inputs, passes }
}
