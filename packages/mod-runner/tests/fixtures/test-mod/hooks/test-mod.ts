// The `--test` fixture: one clean hook, one throwing hook, a renderer whose
// buttons include a failing press callback, an op-invoking hook, and one
// registration against an event this host never raises.

export function register(on) {
  // Clean: reads a `$` op and answers an object.
  on('turn.start', async ($, e) => ({ id: await $.session.id(), turnId: e.turnId }))

  // Throws: the report's failure row.
  on('tool.call', () => {
    throw new Error('exploded')
  })

  // Draws two buttons: one press resolves, one throws.
  on('ui.render', async $ => {
    const ui = $.ui.resolve()
    return ui.Box({
      children: [
        ui.Button({ label: 'Go', onPress: () => 'ok' }),
        ui.Button({ label: 'Break', onPress: () => { throw new Error('press exploded') } }),
      ],
    })
  })

  // Fire-and-forget `$` op for the ops section.
  on('prompt.submit', async $ => {
    $.ui.log('hi from test-mod')
    return { text: 'rewritten' }
  })

  // Registered but never raised: a warning row, not a failure.
  on('telemetry.mark', () => ({}))
}
