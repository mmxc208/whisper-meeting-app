import { describe, expect, it } from 'vitest'
import { describeLevel, summarizeLevels, toDb } from '../src/inputProbe'

describe('summarizeLevels', () => {
  it('folds windowed reports into one RMS and peak per channel', () => {
    const levels = summarizeLevels([
      { type: 'levels', channels: 3, frames: 100, sumSquares: [25, 0, 1], peaks: [0.5, 0, 0.1] },
      { type: 'levels', channels: 3, frames: 100, sumSquares: [25, 0, 1], peaks: [0.4, 0, 0.2] },
    ])
    expect(levels.channels).toBe(3)
    expect(levels.frames).toBe(200)
    expect(levels.rmsDb).toEqual([toDb(0.5), -120, toDb(0.1)])
    expect(levels.peakDb).toEqual([toDb(0.5), -120, toDb(0.2)])
  })

  it('reports no channels when nothing arrived', () => {
    expect(summarizeLevels([])).toEqual({ channels: 0, frames: 0, rmsDb: [], peakDb: [] })
  })
})

describe('toDb and describeLevel', () => {
  it('maps amplitude to dBFS and labels it', () => {
    expect(toDb(1)).toBe(0)
    expect(toDb(0.1)).toBe(-20)
    expect(toDb(0)).toBe(-120)
    expect(describeLevel(-120)).toBe('silent')
    expect(describeLevel(-70)).toBe('silent (noise floor)')
    expect(describeLevel(-50)).toBe('quiet')
    expect(describeLevel(-23)).toBe('active')
  })
})
