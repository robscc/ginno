// The integration fixture: one hook per behavior the broker contract needs —
// a matcher'd chain hook, a renderer with a press button, a double-`next`
// probe, a throwing hook with a `.catch`, and a timer scheduler.

export function register(on, options) {
  // Rewrites the event input, reads a `$` op, and reshapes the result.
  on('tool.call', { tool: /^Bash/ }, async ($, e, next) => {
    const session = await $.session.id()
    const beneath = await next({ ...e, note: `seen:${session}` })
    return { ...beneath, observed: true }
  })

  // Draws a band whose Button carries a callback, and reads the pushed option.
  on('ui.render', async ($, e, next) => {
    const ui = $.ui.resolve(e)
    await next(e)
    return ui.Box({
      flexDirection: 'column',
      children: [
        ui.Text({ color: 'cyan', children: `units=${String(options.units)}` }),
        ui.Button({ label: 'Refresh', onPress: value => `pressed:${String(options.units)}${value === undefined ? '' : `:${String(value)}`}` }),
      ],
    })
  })

  // Calls `next` twice; the runner must send the frame once.
  on('turn.start', async ($, e, next) => {
    const first = await next(e)
    const second = await next(e)
    return { same: second === first }
  })

  // Throws; its `.catch` answers from `next.error`.
  const ending = on('session.end', () => {
    throw new Error('boom')
  })
  ending.catch(async ($, e, next) => ({ handled: next.error?.kind === 'throw' && next.error.message === 'boom' }))

  // Schedules a broker timer whose callback logs through `$`.
  on('session.start', async ($, e) => {
    let bound = undefined
    $.clock.after(5, () => { void bound.ui.log('tick') })
    bound = $
    return { cwd: e.cwd }
  })

  // Hands a later mod's serialized tree through unchanged.
  on('turn.complete', async ($, e, next) => await next(e))

  // Registers against an event this host never raises; the load log warns.
  on('telemetry.mark', () => ({}))
}
