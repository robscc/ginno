// The busy-time clock: it runs while the hook works and pauses around round-trips.

import { describe, expect, test } from 'vitest'
import { BudgetClock } from '../src/clock.ts'

/** A clock whose time the test advances by hand. */
function manualClock() {
  let now = 0
  return {
    now: (): number => now,
    advance(ms: number): void {
      now += ms
    },
  }
}

describe('BudgetClock', () => {
  test('busy time accrues while running and not while paused', () => {
    const time = manualClock()
    const clock = new BudgetClock(1_000, time.now)
    clock.start()
    time.advance(100)
    expect(clock.remainingMs).toBe(900)
    clock.pause()
    time.advance(500)
    expect(clock.remainingMs).toBe(900)
    clock.resume()
    time.advance(100)
    expect(clock.remainingMs).toBe(800)
    clock.stop()
  })

  test('nested pauses count', () => {
    const time = manualClock()
    const clock = new BudgetClock(1_000, time.now)
    clock.start()
    clock.pause()
    clock.pause()
    time.advance(200)
    clock.resume()
    // Still one pause short of running, and none of the paused 200 ms counted.
    expect(clock.remainingMs).toBe(1_000)
    clock.resume()
    time.advance(100)
    expect(clock.remainingMs).toBe(900)
    clock.stop()
  })

  test('remainingMs never goes below zero', () => {
    const time = manualClock()
    const clock = new BudgetClock(100, time.now)
    clock.start()
    time.advance(1_000)
    expect(clock.remainingMs).toBe(0)
    clock.stop()
  })

  test('stop freezes the books', () => {
    const time = manualClock()
    const clock = new BudgetClock(1_000, time.now)
    clock.start()
    time.advance(300)
    clock.stop()
    time.advance(5_000)
    expect(clock.remainingMs).toBe(700)
  })
})
