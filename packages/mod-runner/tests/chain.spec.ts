// The protocol-level integration test: a real loader + HookRuntime driven by
// the MockBroker, frame by frame — the runner half of the DSH chain specs,
// over the wire contract the broker serves (crates/mod-broker/src/protocol.rs):
// an event frame carries an id, and the hook's answer is its `result` frame.

import { describe, expect, test } from 'vitest'
import { fileURLToPath } from 'node:url'
import { MockBroker, BrokerError, asResult } from './harness.ts'
import type { Frame } from '../src/frames.ts'
import type { ResultFrame } from '../src/frames.ts'

const SAMPLE_MOD = fileURLToPath(new URL('./fixtures/sample-mod', import.meta.url))
const MINIMAL_MOD = fileURLToPath(new URL('./fixtures/minimal-mod', import.meta.url))

let nextEventId = 100

const eventFrame = (event: string, invocation: string, payload: unknown, deadlineMs = 10_000): Frame => {
  nextEventId += 1
  return { kind: 'event', id: nextEventId, event, session: 's-1', invocation, payload, deadlineMs }
}

/** Await the runner's `result` for one event frame, flattened for the assertions. */
async function takeAnswer(broker: MockBroker, event: Frame): Promise<ResultFrame> {
  const frame = await broker.take(candidate => candidate.kind === 'result' && candidate['id'] === event['id'])
  return asResult(frame)
}

describe('load and handshake', () => {
  test('loaded → register → hooks-registered with serialized matchers, and `start` back', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start({ units: 'metric' })
    const loaded = await broker.take(frame => frame.kind === 'call' && frame['ns'] === 'runner' && frame['method'] === 'loaded')
    expect(loaded['args']).toEqual({ name: 'sample-mod', version: '0.1.0', userConfig: {} })
    const registered = await broker.take(frame => frame.kind === 'call' && frame['ns'] === 'runner' && frame['method'] === 'hooks-registered')
    expect(registered['args']).toEqual({
      hooks: [
        { event: 'tool.call', matcher: { tool: { regexp: '^Bash', flags: '' } } },
        { event: 'ui.render' },
        { event: 'turn.start' },
        { event: 'session.end' },
        { event: 'session.start' },
        { event: 'turn.complete' },
        { event: 'telemetry.mark' },
      ],
    })
    await broker.close()
  })

  test('the runner pings the broker on the interval', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    const ping = await broker.take(frame => frame.kind === 'notify' && frame['method'] === 'ping', 1_000)
    expect(ping['method']).toBe('ping')
    await broker.close()
  })

  test('stdin EOF resolves the exit', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    expect(await broker.close()).toBeUndefined()
  })
})

describe('event chains', () => {
  test('event → $ call → next → result, with the rewrite and answer carried', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('tool.call', 'i-1', { tool: 'Bash', command: 'ls' })
    broker.send(event)
    const idCall = await broker.take(frame => frame.kind === 'call' && frame['ns'] === 'session')
    // The call frames name the mod, echo the session, and carry the invocation (§6.1).
    expect([idCall['ns'], idCall['method'], idCall['invocation'], idCall['mod'], idCall['session']])
      .toEqual(['session', 'id', 'i-1', 'sample-mod', 's-1'])
    broker.replies(frame => (frame.kind === 'call' ? 's-9' : frame['e']))
    const next = await broker.take(frame => frame.kind === 'next')
    expect(next['invocation']).toBe('i-1')
    expect(next['e']).toEqual({ tool: 'Bash', command: 'ls', note: 'seen:s-9' })
    const answer = await takeAnswer(broker, event)
    expect(answer.ok).toBe(true)
    expect(answer.value).toEqual({ tool: 'Bash', command: 'ls', note: 'seen:s-9', observed: true })
    await broker.close()
  })

  test('a second next() sends no second frame', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('turn.start', 'i-2', { text: 'hi' })
    broker.send(event)
    await broker.take(frame => frame.kind === 'next')
    const answer = await takeAnswer(broker, event)
    expect(broker.sent.filter(frame => frame.kind === 'next')).toHaveLength(1)
    expect(answer.value).toEqual({ same: true })
    await broker.close()
  })

  test('a hook failure answers hook-throw, and the catch-call rides as a call', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('session.end', 'i-3', { sessionId: 's-9' })
    broker.send(event)
    const failed = await takeAnswer(broker, event)
    expect(failed.ok).toBe(false)
    expect(failed.code).toBe('hook-throw')
    expect(String(failed.message)).toContain('boom')
    // The broker carries the failure on the catch-call frame; it becomes `next.error`.
    broker.send({ kind: 'catch-call', id: 7, invocation: 'i-3', event: 'session.end', failure: { kind: 'throw', message: 'boom' } })
    const caught = asResult(await broker.take(frame => frame.kind === 'result' && frame['id'] === 7))
    expect(caught.ok).toBe(true)
    expect(caught.value).toEqual({ handled: true })
    await broker.close()
  })

  test('a failure with no catch handler answers no-catch', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('turn.complete', 'i-3b', { turnId: 't-9', answer: 'x' })
    // Fail `next`: the beneath error rides verbatim — the broker matches it by code+message.
    broker.replies(frame => (frame.kind === 'next' ? new Error('beneath exploded') : undefined))
    broker.send(event)
    const failed = await takeAnswer(broker, event)
    expect(failed.ok).toBe(false)
    expect(failed.message).toContain('beneath exploded')
    broker.send({ kind: 'catch-call', id: 8, invocation: 'i-3b', event: 'turn.complete', failure: { kind: 'throw', message: 'beneath exploded' } })
    const noCatch = asResult(await broker.take(frame => frame.kind === 'result' && frame['id'] === 8))
    expect(noCatch.ok).toBe(false)
    expect(noCatch.code).toBe('no-catch')
    await broker.close()
  })

  test('a next() protocol error the hook rethrows rides verbatim', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('tool.call', 'i-3c', { tool: 'Bash', command: 'ls' })
    broker.replies(frame => (frame.kind === 'call' ? 's-9'
      : new BrokerError('rewrite-refused', 'rerouted the call from Bash to Read')))
    broker.send(event)
    const failed = await takeAnswer(broker, event)
    expect(failed.ok).toBe(false)
    expect(failed.code).toBe('rewrite-refused')
    expect(failed.message).toBe('rerouted the call from Bash to Read')
    await broker.close()
  })

  test('a no-result answer skips the hook', async () => {
    const broker = new MockBroker(MINIMAL_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('session.start', 'i-4', { cwd: '/x' })
    broker.send(event)
    const skipped = await takeAnswer(broker, event)
    expect(skipped.ok).toBe(false)
    expect(skipped.code).toBe('no-result')
    await broker.close()
  })

  test('an event no hook selects answers no-hook instead of dying', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('ui.press', 'i-5', { generation: 1, actionId: 'a' })
    broker.send(event)
    const missed = await takeAnswer(broker, event)
    expect(missed.ok).toBe(false)
    expect(missed.code).toBe('no-hook')
    await broker.close()
  })

  test('a hook-timeout notify abandons the invocation', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    // `session.end` throws immediately; abandon it before the hook even settles.
    const event = eventFrame('session.end', 'i-5b', { sessionId: 's-9' }, 10_000)
    broker.send(event)
    broker.send({ kind: 'notify', method: 'hook-timeout', args: { invocation: 'i-5b' } })
    const answer = await takeAnswer(broker, event)
    expect(answer.ok).toBe(false)
    expect(String(answer.message)).toContain('ran past its')
    // The hook's own throw lands afterwards and changes nothing: exactly one result went out.
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(broker.sent.filter(frame => frame.kind === 'result' && frame['id'] === event['id'])).toHaveLength(1)
    await broker.close()
  })
})

describe('surfaces and presses', () => {
  test('a ui.render answer serializes with runner-minted action ids, and ui.press runs the callback', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start({ units: 'metric' })
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('ui.render', 'i-6', { component: 'AbovePrompt', surface: 'AbovePrompt', props: {}, viewport: { columns: 80 } })
    broker.send(event)
    const answer = await takeAnswer(broker, event)
    expect(answer.value).toEqual([
      {
        type: 'Box',
        props: { flexDirection: 'column' },
        children: [
          { type: 'Text', props: { color: 'cyan' }, children: ['units=metric'] },
          { type: 'Button', props: { label: 'Refresh' }, children: [], actionId: 'sample-mod:a0' },
        ],
      },
    ])
    broker.send({ kind: 'call', id: 41, ns: 'ui', method: 'press', args: { actionId: 'sample-mod:a0', generation: 1 }, session: 's-1', mod: 'sample-mod' })
    const pressed = asResult(await broker.take(frame => frame.kind === 'result' && frame['id'] === 41))
    expect(pressed.ok).toBe(true)
    expect(pressed.value).toBe('pressed:metric')
    broker.send({ kind: 'call', id: 42, ns: 'ui', method: 'press', args: { actionId: 'sample-mod:a5', generation: 1 }, session: 's-1', mod: 'sample-mod' })
    const missed = asResult(await broker.take(frame => frame.kind === 'result' && frame['id'] === 42))
    expect(missed.ok).toBe(false)
    expect(missed.code).toBe('not-found')
    await broker.close()
  })

  test('a serialized tree handed up from next passes through unmarked', async () => {
    const through = [{ type: 'Text', props: { color: 'red' }, children: ['from a later mod'] }]
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    broker.replies(frame => (frame.kind === 'next' ? through : undefined))
    const event = eventFrame('turn.complete', 'i-7', { turnId: 't-1', answer: 'x' })
    broker.send(event)
    const answer = await takeAnswer(broker, event)
    expect(answer.value).toEqual(through)
    await broker.close()
  })
})

describe('broker timers', () => {
  test('clock.after registers at the broker; the clock.fire call runs the closure to completion', async () => {
    const broker = new MockBroker(SAMPLE_MOD)
    broker.start()
    await broker.take(frame => frame.kind === 'call' && frame['method'] === 'hooks-registered')
    const event = eventFrame('session.start', 'i-8', { cwd: '/x' })
    broker.send(event)
    const timer = await broker.take(frame => frame.kind === 'call' && frame['ns'] === 'clock')
    expect(timer['method']).toBe('after')
    broker.replies(frame => (frame.kind === 'call' && frame['method'] === 'after' ? { timer: 't-1' } : undefined))
    // Let the registration round-trip land before the firing call does (frames share one pipe, in order).
    await new Promise(resolve => setTimeout(resolve, 20))
    broker.send({ kind: 'call', id: 51, ns: 'clock', method: 'fire', args: { timer: 't-1' }, session: 's-1', mod: 'sample-mod' })
    const log = await broker.take(frame => frame.kind === 'call' && frame['ns'] === 'ui' && frame['method'] === 'log')
    expect(log['args']).toEqual({ text: 'tick', to: 'transcript' })
    const ack = asResult(await broker.take(frame => frame.kind === 'result' && frame['id'] === 51))
    expect(ack.ok).toBe(true)
    await broker.close()
  })
})
