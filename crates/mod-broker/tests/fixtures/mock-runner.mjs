#!/usr/bin/env node
// Mock mod runner for the broker's integration tests: a zero-dependency Node
// process speaking the broker↔runner protocol (newline-JSON on stdio). Each
// fixture mod directory carries a `scenario.json` describing its hooks and
// how each hook behaves; behaviors cover the chain semantics the tests port
// from DSH's chain.spec (next-once, skip rules, catch sharing, timeouts,
// crashes, `$` calls).
//
// Usage: node mock-runner.mjs --mod <dir>
//
// scenario.json:
// {
//   "hooks": [{
//     "event": "tool.call",          // registration (exact name, "*", "ns.*")
//     "matcher": {"tool": "Bash"},   // optional; serialized matcher form
//     "behavior": "next",            // see runBehavior below
//     "ePatch": {"tool": "Edit"},    // deep-merged into the input for next
//     "answer": {"deny": "no"},      // the hook's own answer (default {})
//     "message": "boom",             // for throw behaviors
//     "sleepMs": 100,                // for sleep_* behaviors
//     "calls": [["state.get", {"plugin": "a", "key": "v"}]],  // $ calls first
//     "catch": "catch_answer",       // .catch handler behavior
//     "catchAnswer": {"deny": "failed closed"}
//   }],
//   "crashOnFire": false             // clock.fire: exit instead of answering
// }

import { readFileSync, appendFileSync } from 'node:fs'
import { join } from 'node:path'

const modDir = process.argv[process.argv.indexOf('--mod') + 1]
const scenario = JSON.parse(readFileSync(join(modDir, 'scenario.json'), 'utf8'))

let nextId = 1
const pending = new Map()
let buffer = ''

function send(frame) {
  process.stdout.write(JSON.stringify(frame) + '\n')
}

function trace(line) {
  try {
    appendFileSync(join(modDir, 'trace.log'), line + '\n')
  } catch {
    // best effort
  }
}

function call(ns, method, args, invocation, session) {
  const id = nextId++
  const frame = { v: 1, kind: 'call', id, ns, method, args }
  if (invocation !== undefined && invocation !== null) frame.invocation = invocation
  if (session !== undefined && session !== null) frame.session = session
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
    send(frame)
  })
}

function deepMerge(base, patch) {
  if (patch === undefined || patch === null) return structuredClone(base)
  if (typeof base !== 'object' || base === null || typeof patch !== 'object' || Array.isArray(base)) {
    return structuredClone(patch)
  }
  const out = structuredClone(base)
  for (const [key, value] of Object.entries(patch)) {
    out[key] = typeof value === 'object' && value !== null && !Array.isArray(value)
      ? deepMerge(out[key], value)
      : structuredClone(value)
  }
  return out
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

// ---- hook behaviors (the chain-semantics vocabulary the tests use) ----

async function runBehavior(hook, frame, kind) {
  const input = frame.payload
  const session = frame.session
  const behavior = kind === 'catch' ? hook.catch : hook.behavior
  const answerOf = () => (kind === 'catch' ? hook.catchAnswer : hook.answer)

  const doNext = async () => {
    const e = deepMerge(input, hook.ePatch)
    const id = nextId++
    const result = await new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject })
      send({ v: 1, kind: 'next', id, invocation: frame.invocation, e })
    })
    return result // {ok, value} or {ok:false, code, message}
  }

  switch (behavior) {
    case 'answer':
    case undefined: {
      // Default: report the `$` calls' results if any were asked, else answer.
      const results = await runCalls(hook, frame)
      const answer = answerOf()
      if (results !== undefined) {
        return { value: results.map((result) => (result.ok ? result.value : { error: result.code })) }
      }
      return { value: answer === undefined ? {} : answer }
    }
    case 'answer_null':
      await runCalls(hook, frame)
      return { value: null }
    case 'no_result':
      return { noResult: true }
    case 'throw':
      return { thrown: hook.message ?? 'boom' }
    case 'next': {
      await runCalls(hook, frame)
      const result = await doNext()
      void session
      if (!result.ok) return { rethrow: result }
      const answer = answerOf()
      return { value: answer === undefined ? result.value : answer }
    }
    case 'next_rethrow': {
      const result = await doNext()
      if (!result.ok) return { rethrow: result } // protocol errors propagate verbatim
      return { value: result.value }
    }
    case 'next_then_throw': {
      const result = await doNext()
      if (!result.ok) return { rethrow: result }
      return { thrown: hook.message ?? 'after' }
    }
    case 'eager_next': {
      void doNext().catch(() => {})
      return { value: answerOf() ?? { deny: 'answered first' } }
    }
    case 'next_twice': {
      const [first, second] = await Promise.all([doNext(), doNext()])
      trace(`${modName}:next_twice same=${first.ok === second.ok && JSON.stringify(first) === JSON.stringify(second)}`)
      if (!first.ok) return { rethrow: first }
      return { value: { same: JSON.stringify(first) === JSON.stringify(second), beneath: first.value } }
    }
    case 'sleep_next': {
      await sleep(hook.sleepMs ?? 100)
      const result = await doNext()
      if (!result.ok) return { rethrow: result }
      return { value: result.value }
    }
    case 'sleep_answer': {
      await sleep(hook.sleepMs ?? 100)
      return { value: answerOf() ?? {} }
    }
    case 'render_state': {
      // Read a state slot through `$`, then answer a band tree naming it.
      const ref = hook.stateRef ?? { plugin: modName, key: 'v' }
      const result = await call('state', 'get', { plugin: ref.plugin, key: ref.key }, frame.invocation, frame.session)
      const value = result.value?.value ?? null
      const tree = [{ type: 'Text', props: {}, children: [`count ${value}`] }]
      return { value: tree }
    }
    case 'render_button': {
      const tree = [{ type: 'Button', props: { label: 'More' }, children: [], actionId: `${modName}:a0` }]
      return { value: tree }
    }
    case 'store_limit': {
      const big = 'x'.repeat(5 * 1024 * 1024)
      const result = await call('store', 'set', { key: 'big', value: big }, frame.invocation, frame.session)
      return { value: { ok: result.ok, code: result.code ?? null } }
    }
    case 'single_call': {
      const [ns, method, args] = hook.call
      const result = await call(ns, method, args, frame.invocation, frame.session)
      // The traced result keeps the call observable even when the runtime
      // that raised the event disconnected before the result frame could go
      // back (the ui.ask abort test reads it).
      trace(`${modName}:call:${ns}.${method}:${JSON.stringify(result)}`)
      return result.ok
        ? { value: { ok: true, value: result.value ?? null } }
        : { value: { ok: false, code: result.code, message: result.message } }
    }
    case 'calls_collect': {
      const results = (await runCalls(hook, frame)) ?? []
      return {
        value: results.map((result) => (result.ok ? { ok: true, value: result.value ?? null } : { ok: false, code: result.code, message: result.message })),
      }
    }
    case 'crash':
      process.exit(1)
      return {}
    default:
      return { thrown: `unknown behavior ${behavior}` }
  }
}

// Raw call results ({ok, value} or {ok, code, message}); callers map.
async function runCalls(hook, frame) {
  if (!hook.calls) return undefined
  const results = []
  for (const [ns, method, args] of hook.calls) {
    results.push(await call(ns, method, args, frame.invocation, frame.session))
  }
  return results
}

// ---- stdio loop ----

const modName = modDir.split('/').filter(Boolean).pop()

process.stdin.setEncoding('utf8')
process.stdin.on('data', (chunk) => {
  buffer += chunk
  let index
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index)
    buffer = buffer.slice(index + 1)
    if (line.trim()) handle(line)
  }
})
process.stdin.on('end', () => process.exit(0))

function handle(line) {
  let frame
  try {
    frame = JSON.parse(line)
  } catch {
    return
  }
  if (frame.kind === 'result') {
    const waiter = pending.get(frame.id)
    if (waiter) {
      pending.delete(frame.id)
      if (frame.ok) waiter.resolve({ ok: true, value: frame.value ?? null })
      else waiter.resolve({ ok: false, code: frame.code, message: frame.message })
    }
    return
  }
  if (frame.kind === 'event') {
    handleEvent(frame)
    return
  }
  if (frame.kind === 'catch-call') {
    handleCatch(frame)
    return
  }
  if (frame.kind === 'call') {
    handleCall(frame)
  }
  // notify (ping etc.): nothing to do
}

function replyResult(id, value) {
  send({ v: 1, kind: 'result', id, ok: true, value })
}

function replyError(id, code, message) {
  send({ v: 1, kind: 'result', id, ok: false, code, message })
}

function hookFor(event) {
  return (scenario.hooks ?? []).find(
    (candidate) =>
      candidate.event === event ||
      candidate.event === '*' ||
      (typeof candidate.event === 'string' && candidate.event.endsWith('.*') && event.startsWith(candidate.event.slice(0, -1)))
  )
}

function handleEvent(frame) {
  const hook = hookFor(frame.event) ?? {}
  trace(`${modName}:${frame.event}:begin`)
  runBehavior(hook ?? {}, frame, 'hook')
    .then((outcome) => {
      if (outcome.rethrow) {
        // Beneath failed: protocol errors propagate verbatim (code+message).
        send({ v: 1, kind: 'result', id: frame.id, ok: false, code: outcome.rethrow.code, message: outcome.rethrow.message })
      } else if (outcome.thrown !== undefined) {
        replyError(frame.id, 'hook-throw', outcome.thrown)
      } else if (outcome.noResult) {
        replyError(frame.id, 'no-result', 'returned no result')
      } else {
        replyResult(frame.id, outcome.value)
      }
      trace(`${modName}:${frame.event}:end`)
    })
    .catch((error) => replyError(frame.id, 'hook-throw', String(error?.message ?? error)))
}

function handleCatch(frame) {
  const hook = hookFor(frame.event) ?? {}
  if (!hook.catch) {
    replyError(frame.id, 'no-catch', 'no catch handler')
    return
  }
  trace(`${modName}:${frame.event}:catch`)
  runBehavior(hook, frame, 'catch')
    .then((outcome) => {
      if (outcome.rethrow) replyError(frame.id, outcome.rethrow.code, outcome.rethrow.message)
      else if (outcome.thrown !== undefined) replyError(frame.id, 'hook-throw', outcome.thrown)
      else if (outcome.noResult) replyError(frame.id, 'no-result', 'returned no result')
      else replyResult(frame.id, outcome.value)
    })
    .catch((error) => replyError(frame.id, 'hook-throw', String(error?.message ?? error)))
}

function handleCall(frame) {
  const { ns, method, args, id } = frame
  if (ns === 'runner' && method === 'hooks-registered') {
    if (started) {
      replyError(id, 'error', 'already started')
      return
    }
    started = true
    const hooks = (scenario.hooks ?? []).map((hook) => {
      const entry = { event: hook.event }
      if (hook.matcher) entry.matcher = hook.matcher
      return entry
    })
    replyResult(id, { hooks })
    // Heartbeat per the protocol: every ~1s in tests.
    // Heartbeat: ref'd on purpose — the broker owns this process's lifetime.
    setInterval(() => {
      console.error('MOCK PING TICK')
      send({ v: 1, kind: 'notify', method: 'ping' })
    }, 1000)
    return
  }
  if (ns === 'clock' && method === 'fire') {
    trace(`${modName}:timer:${args.timer}`)
    if (scenario.crashOnFire) process.exit(1)
    replyResult(id, null)
    return
  }
  if (ns === 'ui' && method === 'press') {
    // A pane Input/Select press carries the user's value; keep it observable.
    const value = 'value' in args ? `:${JSON.stringify(args.value)}` : ''
    trace(`${modName}:press:${args.actionId}${value}`)
    replyResult(id, null)
    return
  }
  replyError(id, 'no-implementation', `mock runner does not implement ${ns}.${method}`)
}

// Register with the broker and heartbeat until it tears us down. The
// handshake ack resolves through `pending` like any other round-trip.
const handshakeId = nextId++
pending.set(handshakeId, {
  resolve: () => {},
  reject: () => {},
})
send({
  v: 1,
  kind: 'call',
  id: handshakeId,
  ns: 'runner',
  method: 'hooks-registered',
  args: { hooks: (scenario.hooks ?? []).map((hook) => ({ event: hook.event, ...(hook.matcher ? { matcher: hook.matcher } : {}) })) },
})
setInterval(() => {
  send({ v: 1, kind: 'notify', method: 'ping' })
}, 1000)
