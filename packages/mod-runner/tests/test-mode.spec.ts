/**
 * The `--test` suite's own specs: run the fixture mod through {@link testMod}
 * and assert the report's hooks / ops / warnings sections and the exit code.
 * @module
 */

import { describe, expect, it } from 'vitest'
import { resolve } from 'node:path'
import { exitCodeOf, renderReport, testMod } from '../src/test-mode.ts'

const modDir = resolve(import.meta.dirname, 'fixtures', 'test-mod')

describe('testMod', () => {
  it('loads the fixture and reports its identity', async () => {
    const report = await testMod(modDir)
    expect(report.loaded).toBe(true)
    expect(report.mod.name).toBe('test-mod')
    expect(report.mod.version).toBe('1.2.3')
  })

  it('runs one synthetic event per registered hook, in order (presses right after their render)', async () => {
    const report = await testMod(modDir)
    expect(report.hooks.map(hook => hook.event)).toEqual([
      'turn.start', 'tool.call', 'ui.render', 'ui.press{test-mod:a1}', 'prompt.submit', 'telemetry.mark',
    ])
  })

  it('marks the throwing hook as failed with its message, the rest as ok', async () => {
    const report = await testMod(modDir)
    const throwing = report.hooks.find(hook => hook.event === 'tool.call')
    expect(throwing?.ok).toBe(false)
    expect(throwing?.error?.code).toBe('hook-throw')
    expect(throwing?.error?.message).toContain('exploded')
    for (const event of ['turn.start', 'ui.render', 'prompt.submit', 'telemetry.mark']) {
      expect(report.hooks.find(hook => hook.event === event)?.ok, event).toBe(true)
    }
  })

  it('presses every button of the ui.render tree and reports the failing press', async () => {
    const report = await testMod(modDir)
    expect(report.presses.tried).toBe(2)
    expect(report.presses.failed).toBe(1)
    const failedPress = report.hooks.find(hook => hook.event.startsWith('ui.press{') && !hook.ok)
    expect(failedPress?.error?.message).toContain('press exploded')
  })

  it('counts the $ ops the hooks invoked', async () => {
    const report = await testMod(modDir)
    const ops = report.ops.map(stat => stat.op)
    expect(ops).toContain('session.id')
    expect(ops).toContain('ui.log')
    // `ui.resolve` is served locally (elements constructor), never framed.
    expect(ops).not.toContain('ui.resolve')
  })

  it('flags the never-raised event as an unserved warning', async () => {
    const report = await testMod(modDir)
    expect(report.unservedEvents).toEqual(['telemetry.mark'])
    expect(report.unimplementedOps).toEqual([])
  })

  it('fails the run (exit 1) while a hook throws, and renders the failure', async () => {
    const report = await testMod(modDir)
    expect(exitCodeOf(report)).toBe(1)
    const text = renderReport(report)
    expect(text).toContain('mod test report — test-mod v1.2.3')
    expect(text).toContain('✗ tool.call')
    expect(text).toContain('✓ turn.start')
    expect(text).toContain('session.id ×')
    expect(text).toContain('warning: events registered but never raised')
    expect(text).toContain('result: FAIL (2 hook failures)')
  })

  it('exits 0 when every hook runs clean', async () => {
    // A passing report assembled by hand: no fixture of all-clean hooks needed.
    const report = await testMod(modDir)
    const clean = {
      ...report,
      hooks: report.hooks
        .filter(hook => !hook.event.startsWith('ui.press{'))
        .map(hook => (hook.event === 'tool.call' ? { ...hook, ok: true, error: undefined } : hook)),
      presses: { tried: 2, failed: 0 },
    }
    expect(exitCodeOf(clean)).toBe(0)
    expect(renderReport(clean)).toContain('result: OK')
  })

  it('reports a load failure for a directory with no mod', async () => {
    const report = await testMod(resolve(import.meta.dirname, 'fixtures', 'no-such-mod'))
    expect(report.loaded).toBe(false)
    expect(report.loadError).toBeTruthy()
    expect(exitCodeOf(report)).toBe(1)
    expect(renderReport(report)).toContain('mod not loaded')
  })
})
