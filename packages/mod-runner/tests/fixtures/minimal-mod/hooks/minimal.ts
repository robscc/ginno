// The no-result fixture: a hook that answers with nothing is skipped, not drawn empty.

export function register(on) {
  on('session.start', () => undefined)
}
