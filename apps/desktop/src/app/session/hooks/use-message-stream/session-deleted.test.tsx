import type { GatewayEvent } from '@hermes/shared'
import { QueryClient } from '@tanstack/react-query'
import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { clearAllPrompts, sessionApprovalRequest, setApprovalRequest } from '@/store/prompts'
import { $activeSessionId } from '@/store/session'
import { $sessionStates, $sessionTiles, publishSessionState } from '@/store/session-states'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

// `session.deleted`: a conversation was deleted (from this window or any other
// surface). The backend already closed its live sessions and removed its rows;
// every window drops what it still holds for it.

let stream: MessageStreamHarness

const deleted = (runtimeIds: string[], storedIds: string[] = ['stored-1']) =>
  act(() =>
    stream.handleEvent({
      payload: { runtime_session_ids: runtimeIds, stored_session_ids: storedIds },
      session_id: '',
      type: 'session.deleted'
    } as GatewayEvent)
  )

beforeEach(() => {
  stream = renderMessageStream('session-active', { activeGatewayProfile: 'compass', queryClient: new QueryClient() })
  $sessionStates.set({})
  $sessionTiles.set([])
  $activeSessionId.set(null)
  clearAllPrompts()
})

afterEach(() => {
  cleanup()
  $sessionStates.set({})
  $sessionTiles.set([])
  $activeSessionId.set(null)
  clearAllPrompts()
  vi.restoreAllMocks()
})

describe('session.deleted', () => {
  it('drops the deleted runtimes and leaves every other live session alone', () => {
    publishSessionState('live-gone', createClientSessionState('stored-1'))
    publishSessionState('live-kept', createClientSessionState('stored-2'))
    setApprovalRequest({ command: 'rm stale', description: 'stale request', sessionId: 'live-gone' })
    setApprovalRequest({ command: 'rm kept', description: 'kept request', sessionId: 'live-kept' })

    deleted(['live-gone'])

    expect($sessionStates.get()['live-gone']).toBeUndefined()
    expect($sessionStates.get()['live-kept']).toBeDefined()
    expect(sessionApprovalRequest('live-gone').get()).toBeNull()
    expect(sessionApprovalRequest('live-kept').get()?.command).toBe('rm kept')
  })

  it('unbinds a tile holding a deleted runtime', () => {
    $sessionTiles.set([
      { runtimeId: 'live-gone', storedSessionId: 'stored-1' },
      { runtimeId: 'live-kept', storedSessionId: 'stored-2' }
    ])

    deleted(['live-gone'])

    const tiles = $sessionTiles.get()
    expect(tiles.find(t => t.storedSessionId === 'stored-1')?.runtimeId).toBeUndefined()
    expect(tiles.find(t => t.storedSessionId === 'stored-2')?.runtimeId).toBe('live-kept')
  })

  it('is a no-op for a conversation this window never opened', () => {
    publishSessionState('live-a', createClientSessionState('stored-a'))

    deleted([], ['stored-elsewhere'])

    expect(Object.keys($sessionStates.get())).toEqual(['live-a'])
  })
})
