import type { SessionInfo } from '@/types/hermes'

export interface SessionSizeFormatters {
  messageCount: (count: number) => string
  turnCount: (count: number) => string
}

/** How big a conversation is, as the user sees it: the prompts they typed.
 *  `message_count` counts every stored row (each tool call and tool result
 *  too), so it is only the fallback for backends that predate `turn_count`.
 *  Null when there is nothing to show. */
export function sessionSizeLabel(session: SessionInfo, fmt: SessionSizeFormatters): null | string {
  if (session.turn_count !== undefined) {
    return session.turn_count > 0 ? fmt.turnCount(session.turn_count) : null
  }

  return session.message_count > 0 ? fmt.messageCount(session.message_count) : null
}
