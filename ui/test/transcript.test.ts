import { describe, expect, it } from 'vitest'
import { visibleTranscriptRows, visibleTranscriptSegments } from '../src/transcript'

const speech = (id: number, start: number, end: number, text: string, epoch = 1) => ({
  id,
  epoch,
  audio_start_sample: start * 16000,
  audio_end_sample: end * 16000,
  coverage_kind: 'speech',
  text,
})

describe('visibleTranscriptSegments', () => {
  it('hides empty decode coverage while retaining user-relevant rows', () => {
    const segments = [
      { id: 1, coverage_kind: 'speech', text: 'Hello' },
      { id: 2, coverage_kind: 'empty_decode', text: '' },
      { id: 3, coverage_kind: 'silence', text: '' },
    ]

    expect(visibleTranscriptSegments(segments)).toEqual([segments[0], segments[2]])
  })
})

describe('visibleTranscriptRows', () => {
  it('merges nearby speech in one thirty-second bucket into a paragraph', () => {
    const segments = [
      speech(1, 11, 14, 'Hello'),
      speech(2, 15, 18, 'from'),
      speech(3, 19, 22, 'Whisper Meeting.'),
    ]

    expect(visibleTranscriptRows(segments)).toEqual([
      { kind: 'speech', key: '1', text: 'Hello from Whisper Meeting.', timestamp: '00:11' },
    ])
  })

  it('starts a new paragraph after a gap longer than two seconds', () => {
    const segments = [speech(1, 1, 4, 'First.'), speech(2, 7, 9, 'Second.')]

    expect(visibleTranscriptRows(segments)).toEqual([
      { kind: 'speech', key: '1', text: 'First.', timestamp: '00:01' },
      { kind: 'speech', key: '2', text: 'Second.', timestamp: undefined },
    ])
  })

  it('starts a new paragraph and timestamp when the thirty-second bucket changes', () => {
    const segments = [speech(1, 27, 30, 'First.'), speech(2, 30, 32, 'Second.')]

    expect(visibleTranscriptRows(segments)).toEqual([
      { kind: 'speech', key: '1', text: 'First.', timestamp: '00:27' },
      { kind: 'speech', key: '2', text: 'Second.', timestamp: '00:30' },
    ])
  })

  it('does not merge across capture epochs', () => {
    const segments = [speech(1, 0, 10, 'Before pause.'), speech(2, 0, 2, 'After resume.', 2)]

    expect(visibleTranscriptRows(segments)).toEqual([
      { kind: 'speech', key: '1', text: 'Before pause.', timestamp: '00:00' },
      { kind: 'speech', key: '2', text: 'After resume.', timestamp: undefined },
    ])
  })

  it('joins consecutive Chinese text without inserting artificial spaces', () => {
    const segments = [speech(1, 0, 2, '这是'), speech(2, 2, 4, '一段中文。')]

    expect(visibleTranscriptRows(segments)[0]).toMatchObject({ text: '这是一段中文。' })
  })

  it('retains a silence row and prevents paragraphs from crossing it', () => {
    const silence = {
      id: 2,
      epoch: 1,
      audio_start_sample: 2 * 16000,
      audio_end_sample: 3 * 16000,
      coverage_kind: 'silence',
      text: '',
    }
    const segments = [speech(1, 0, 2, 'Before.'), silence, speech(3, 3, 5, 'After.')]

    expect(visibleTranscriptRows(segments)).toEqual([
      { kind: 'speech', key: '1', text: 'Before.', timestamp: '00:00' },
      { kind: 'silence', key: '2', segment: silence },
      { kind: 'speech', key: '3', text: 'After.', timestamp: undefined },
    ])
  })

  it('continues elapsed audio time across capture epochs', () => {
    const segments = [
      {
        id: 1,
        epoch: 1,
        audio_start_sample: 0,
        audio_end_sample: 20 * 16000,
        coverage_kind: 'empty_decode',
        text: '',
      },
      speech(2, 15, 18, 'Continued.', 2),
    ]

    expect(visibleTranscriptRows(segments)).toEqual([
      { kind: 'speech', key: '2', text: 'Continued.', timestamp: '00:35' },
    ])
  })
})
