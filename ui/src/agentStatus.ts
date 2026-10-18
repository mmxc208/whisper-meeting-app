export type AgentStatus = {
  agent_id: string
  state: string
  output_revision: number
}

export const NOTE_AGENT = 'whisper-meeting-note-taker'
export const SKETCH_AGENT = 'whisper-meeting-sketch-artist'

export function agentSnapshotNeedsPolling(agents: AgentStatus[]): boolean {
  return agents.some(agent => !['complete', 'failed'].includes(agent.state))
}

export function agentStateLabel(agent: AgentStatus, meetingState?: string): string {
  const name = agent.agent_id === NOTE_AGENT ? 'notes' : 'sketch'
  switch (agent.state) {
    case 'pending':
      return agent.output_revision ? 'Listening' : 'Starting'
    case 'working':
      return 'Working'
    case 'caught_up':
      return meetingState === 'recording' ? 'Listening' : 'Caught up'
    case 'finalizing':
      return `Finishing ${name}`
    case 'complete':
      return 'Caught up'
    case 'unavailable':
      return 'Unavailable'
    case 'failed':
      return 'Failed'
    default:
      return agent.state
  }
}
