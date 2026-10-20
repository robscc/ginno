// The element vocabulary: construction, validation, serialization, and search.

import { describe, expect, test } from 'vitest'
import { findAll, isUiElement, renderText, serializeTree, treeProblem, uiElements } from '../src/elements.ts'

const elements = (color?: string) => {
  const ui = uiElements()
  return ui.Box({
    flexDirection: 'column',
    children: [
      ui.Text({ color, children: ['hello ', ui.Text({ bold: true, children: 'world' })] }),
      ui.Button({ label: 'Go', onPress: () => 'go' }),
      'tail',
    ],
  })
}

describe('constructors', () => {
  test('elements carry a brand, frozen type and props', () => {
    const tree = elements()
    expect(isUiElement(tree)).toBe(true)
    expect(Object.isFrozen(tree)).toBe(true)
    expect(Object.isFrozen(tree.props)).toBe(true)
  })
})

describe('treeProblem', () => {
  test('a valid tree validates', () => {
    expect(treeProblem(elements())).toBeUndefined()
    expect(treeProblem(null)).toBeUndefined()
    expect(treeProblem([elements(), 'x', 3])).toBeUndefined()
  })

  test('a Button without a string label fails', () => {
    const ui = uiElements()
    expect(treeProblem(ui.Button({ label: 3 as unknown as string }))).toBe('a Button needs a string label')
  })

  test('a Button with a non-function onPress fails', () => {
    const ui = uiElements()
    expect(treeProblem(ui.Button({ label: 'x', onPress: 'go' as unknown as (() => unknown) }))).toBe('a Button onPress must be a function')
  })

  test('a foreign node fails with its type named', () => {
    expect(treeProblem({ nope: true } as unknown as never)).toContain('not an element')
  })
})

describe('serializeTree', () => {
  test('scalars are kept, non-scalars dropped, callbacks held in order, children flattened', () => {
    const held: string[] = []
    const serialized = serializeTree(elements('cyan'), callback => {
      held.push('held')
      void callback
      return `action-${held.length - 1}`
    })
    expect(serialized).toEqual([
      {
        type: 'Box',
        props: { flexDirection: 'column' },
        children: [
          { type: 'Text', props: { color: 'cyan' }, children: ['hello ', { type: 'Text', props: { bold: true }, children: ['world'] }] },
          { type: 'Button', props: { label: 'Go' }, children: [], actionId: 'action-0' },
          'tail',
        ],
      },
    ])
  })

  test('a Button without onPress serializes without an action id', () => {
    const ui = uiElements()
    expect(serializeTree(ui.Button({ label: 'x' }), () => 'a')).toEqual([
      { type: 'Button', props: { label: 'x' }, children: [] },
    ])
  })
})

describe('search and text', () => {
  test('findAll matches by type, text, and label in drawing order', () => {
    const tree = elements()
    expect(findAll(tree, { type: 'Button' })).toHaveLength(1)
    // The Box that draws the text, and both Text runs inside it.
    expect(findAll(tree, { text: 'world' })).toHaveLength(3)
    expect(findAll(tree, { label: /Go/ })).toHaveLength(1)
    expect(findAll(tree, {})).toHaveLength(4)
  })

  test('renderText draws one line per top-level node, child texts joined', () => {
    expect(renderText(elements())).toBe('hello worldGotail')
  })
})

describe('P2 vocabulary: Input/Select/Markdown/Code (2026-10-21)', () => {
  test('constructors produce the new element types', () => {
    const ui = uiElements()
    expect(ui.Input({ value: 'a', placeholder: 'type' }).type).toBe('Input')
    expect(ui.Select({ options: [{ value: 'x' }], onSelect: () => {} }).type).toBe('Select')
    expect(ui.Markdown({ children: '**bold**' }).type).toBe('Markdown')
    expect(ui.Code({ children: 'let x', language: 'ts' }).type).toBe('Code')
  })

  test('treeProblem: Input/Select callback type checks, Markdown/Code string body', () => {
    const ui = uiElements()
    expect(treeProblem(ui.Input({ onInput: 'not-a-fn' as never }))).toContain('Input')
    expect(treeProblem(ui.Select({ options: null as never }))).toContain('options')
    expect(treeProblem(ui.Markdown({ children: 42 as never }))).toContain('string')
    expect(treeProblem(ui.Code({ children: 'ok' }))).toBeUndefined()
  })

  test('serializeTree: callbacks become action ids, text bodies flatten to a string child', () => {
    const ui = uiElements()
    expect(serializeTree(ui.Input({ onInput: () => {} }), () => 'act-1')).toEqual([
      { type: 'Input', props: {}, children: [], actionId: 'act-1' },
    ])
    expect(serializeTree(ui.Code({ children: 'let x', language: 'ts' }), () => 'a')).toEqual([
      { type: 'Code', props: { language: 'ts' }, children: ['let x'] },
    ])
  })
})
