/**
 * The runner's side of the newline-delimited JSON frame protocol that links
 * it to the broker over stdio. Requests the runner sends (`call`, `next`)
 * carry an id and are correlated with the `result` frame that answers them;
 * everything the runner receives for it to act on (event, catch-call, press,
 * timer-callback, notify) is handed to the frame handler.
 * @module
 */

/** Any one frame of the protocol; fields depend on `kind`. */
export interface Frame {
  v?: number
  kind: string
  [field: string]: unknown
}

/** The `{kind:'result'}` frame that answers a request the runner sent. */
export interface ResultFrame extends Frame {
  kind: 'result'
  id: number
  ok: boolean
  value?: unknown
  code?: string
  message?: string
}

function pendingResolver(): PromiseWithResolvers<ResultFrame> {
  return Promise.withResolvers<ResultFrame>()
}

export class FrameConnection {
  private readonly pending = new Map<number, PromiseWithResolvers<ResultFrame>>()
  private nextId = 1
  private lineBuffer = ''
  private started = false
  private closed = false
  /** Called for every inbound frame that is not a `result` for a pending request. */
  frameHandler: ((frame: Frame) => void) | undefined
  /** Called once when stdin ends; the runner's orderly exit hangs from it. */
  closeHandler: (() => void) | undefined

  constructor(
    private readonly input: NodeJS.ReadableStream = process.stdin,
    private readonly output: NodeJS.WritableStream = process.stdout,
  ) {}

  /** Start reading stdin; frames flow to {@link frameHandler} from now on. */
  start(): void {
    if (this.started) return
    this.started = true
    this.input.on('data', (chunk: Buffer | string) => {
      this.lineBuffer += typeof chunk === 'string' ? chunk : chunk.toString('utf8')
      let newline = this.lineBuffer.indexOf('\n')
      while (newline !== -1) {
        const line = this.lineBuffer.slice(0, newline)
        this.lineBuffer = this.lineBuffer.slice(newline + 1)
        this.receiveLine(line)
        newline = this.lineBuffer.indexOf('\n')
      }
    })
    this.input.on('end', () => {
      this.closed = true
      this.closeHandler?.()
    })
    this.input.on('error', (error: unknown) => {
      console.error('mod-runner: stdin error:', error)
      this.closed = true
      this.closeHandler?.()
    })
  }

  private receiveLine(line: string): void {
    if (line.trim().length === 0) return
    let frame: Frame
    try {
      frame = JSON.parse(line) as Frame
    } catch (error: unknown) {
      console.error('mod-runner: dropped an unparseable frame:', error instanceof Error ? error.message : error)
      return
    }
    if (frame.kind === 'result' && typeof frame.id === 'number') {
      const waiter = this.pending.get(frame.id)
      if (waiter !== undefined) {
        this.pending.delete(frame.id)
        waiter.resolve(frame as ResultFrame)
      } else {
        console.error(`mod-runner: a result for unknown request ${String(frame.id)} arrived; dropped`)
      }
      return
    }
    this.frameHandler?.(frame)
  }

  /** Write one frame without expecting an answer. */
  write(frame: Frame): void {
    if (this.closed) return
    this.output.write(`${JSON.stringify({ v: 1, ...frame })}\n`)
  }

  /** One-way notification to the broker. */
  notify(method: string, args?: unknown): void {
    this.write({ kind: 'notify', method, ...(args === undefined ? {} : { args }) })
  }

  /**
   * Send a request (`call` or `next`) and resolve with the `result` frame
   * that answers it. Never rejects; a failed result comes back with
   * `ok:false` for the caller to word.
   */
  request(frame: Omit<Frame, 'v' | 'id' | 'kind'> & { kind: 'call' | 'next' }): Promise<ResultFrame> {
    if (this.closed) return Promise.resolve({ kind: 'result', id: -1, ok: false, code: 'closed', message: 'the broker connection is closed' })
    const id = this.nextId
    this.nextId += 1
    const waiter = pendingResolver()
    this.pending.set(id, waiter)
    this.write({ ...frame, id })
    return waiter.promise
  }

  /** Answer a broker request (press, timer-callback) the runner acted on. */
  respond(id: number, ok: boolean, fields?: { value?: unknown; code?: string; message?: string }): void {
    this.write({ kind: 'result', id, ok, ...fields })
  }

  /**
   * The next inbound frame matching a predicate, for the load handshake.
   * Resolves undefined once the timeout passes.
   */
  waitFor(predicate: (frame: Frame) => boolean, timeoutMs?: number): Promise<Frame | undefined> {
    return new Promise(resolve => {
      const previous = this.frameHandler
      let timer: NodeJS.Timeout | undefined
      const settle = (frame: Frame | undefined): void => {
        if (timer !== undefined) clearTimeout(timer)
        this.frameHandler = previous
        resolve(frame)
      }
      this.frameHandler = frame => {
        if (predicate(frame)) settle(frame)
        else previous?.(frame)
      }
      if (timeoutMs !== undefined) timer = setTimeout(() => settle(undefined), timeoutMs).unref()
    })
  }
}
