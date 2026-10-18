export function visibleTranscriptSegments<T extends { coverage_kind: string }>(
  segments: T[],
): T[] {
  return segments.filter(segment => segment.coverage_kind !== 'empty_decode')
}

type TimedSegment = {
  id: number
  epoch: number
  audio_start_sample: number
  audio_end_sample: number
  coverage_kind: string
  text: string
}

export type TranscriptRow<T> =
  | {
      kind: 'speech'
      key: string
      text: string
      timestamp?: string
    }
  | {
      kind: 'silence'
      key: string
      segment: T
    }

const SAMPLE_RATE = 16_000
const TIMESTAMP_INTERVAL_SAMPLES = 30 * SAMPLE_RATE
const PARAGRAPH_GAP_SAMPLES = 2 * SAMPLE_RATE
const CJK_AT_END = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u
const CJK_AT_START = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u

function formatTimestamp(samples: number): string {
  const seconds = Math.floor(samples / SAMPLE_RATE)
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function appendText(existing: string, next: string): string {
  const left = existing.trimEnd()
  const right = next.trimStart()
  if (!left) return right
  if (!right) return left
  const separator = CJK_AT_END.test(left) && CJK_AT_START.test(right) ? '' : ' '
  return `${left}${separator}${right}`
}

export function visibleTranscriptRows<T extends TimedSegment>(segments: T[]): TranscriptRow<T>[] {
  const epochEnds = new Map<number, number>()
  for (const segment of segments) {
    epochEnds.set(
      segment.epoch,
      Math.max(epochEnds.get(segment.epoch) ?? 0, segment.audio_end_sample),
    )
  }

  const epochOffsets = new Map<number, number>()
  let offset = 0
  for (const epoch of [...epochEnds.keys()].sort((left, right) => left - right)) {
    epochOffsets.set(epoch, offset)
    offset += epochEnds.get(epoch) ?? 0
  }

  const rows: TranscriptRow<T>[] = []
  let paragraph: {
    key: string
    text: string
    timestamp?: string
    epoch: number
    bucket: number
    lastEnd: number
  } | undefined
  let previousSpeechBucket: number | undefined

  const flushParagraph = () => {
    if (!paragraph) return
    rows.push({
      kind: 'speech',
      key: paragraph.key,
      text: paragraph.text,
      timestamp: paragraph.timestamp,
    })
    paragraph = undefined
  }

  for (const segment of segments) {
    if (segment.coverage_kind === 'empty_decode') continue
    if (segment.coverage_kind !== 'speech') {
      flushParagraph()
      rows.push({ kind: 'silence', key: String(segment.id), segment })
      continue
    }

    const globalStart = (epochOffsets.get(segment.epoch) ?? 0) + segment.audio_start_sample
    const bucket = Math.floor(globalStart / TIMESTAMP_INTERVAL_SAMPLES)
    const joinsParagraph = paragraph !== undefined
      && paragraph.epoch === segment.epoch
      && paragraph.bucket === bucket
      && segment.audio_start_sample - paragraph.lastEnd <= PARAGRAPH_GAP_SAMPLES

    if (joinsParagraph && paragraph) {
      paragraph.text = appendText(paragraph.text, segment.text)
      paragraph.lastEnd = Math.max(paragraph.lastEnd, segment.audio_end_sample)
      previousSpeechBucket = bucket
      continue
    }

    flushParagraph()
    paragraph = {
      key: String(segment.id),
      text: segment.text.trim(),
      timestamp: bucket === previousSpeechBucket ? undefined : formatTimestamp(globalStart),
      epoch: segment.epoch,
      bucket,
      lastEnd: segment.audio_end_sample,
    }
    previousSpeechBucket = bucket
  }

  flushParagraph()
  return rows
}
