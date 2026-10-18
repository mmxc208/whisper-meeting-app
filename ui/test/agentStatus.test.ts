import { describe, expect, it } from 'vitest'
import {
  NOTE_AGENT,
  SKETCH_AGENT,
  agentSnapshotNeedsPolling,
  agentStateLabel,
} from '../src/agentStatus'

describe('agentStateLabel', () => {
  it('distinguishes startup, listening and finished states', () => {
    expect(agentStateLabel({ agent_id: NOTE_AGENT, state: 'pending', output_revision: 0 }))
      .toBe('Starting')
    expect(agentStateLabel({ agent_id: NOTE_AGENT, state: 'pending', output_revision: 1 }))
      .toBe('Listening')
    expect(agentStateLabel(
      { agent_id: NOTE_AGENT, state: 'caught_up', output_revision: 1 },
      'recording',
    )).toBe('Listening')
    expect(agentStateLabel(
      { agent_id: NOTE_AGENT, state: 'caught_up', output_revision: 1 },
      'ended',
    )).toBe('Caught up')
  })

  it('uses agent-specific finalization wording', () => {
    expect(agentStateLabel({ agent_id: NOTE_AGENT, state: 'finalizing', output_revision: 2 }))
      .toBe('Finishing notes')
    expect(agentStateLabel({ agent_id: SKETCH_AGENT, state: 'finalizing', output_revision: 2 }))
      .toBe('Finishing sketch')
  })
})

describe('agentSnapshotNeedsPolling', () => {
  it('polls recoverable and changing states', () => {
    for (const state of ['pending', 'working', 'caught_up', 'finalizing', 'unavailable']) {
      expect(agentSnapshotNeedsPolling([
        { agent_id: NOTE_AGENT, state, output_revision: 1 },
      ])).toBe(true)
    }
  })

  it('stops once every agent is terminal', () => {
    expect(agentSnapshotNeedsPolling([
      { agent_id: NOTE_AGENT, state: 'complete', output_revision: 2 },
      { agent_id: SKETCH_AGENT, state: 'failed', output_revision: 1 },
    ])).toBe(false)
  })
})
