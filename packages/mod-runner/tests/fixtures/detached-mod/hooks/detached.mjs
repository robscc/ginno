// A hook that returns BEFORE its detached async calls `$` — next-steps' shape.
// The 30ms delay guarantees the hook settles first (the real mod's fork call
// takes seconds: command.list + model.fork both land well after settle).
export function register(on) {
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    setTimeout(() => {
      void $.session.id().catch(() => {})
    }, 30)
    return result
  })
}
