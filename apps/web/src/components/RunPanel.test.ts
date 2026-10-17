import { describe, expect, it } from 'vitest'
import { parseArg } from './RunPanel'

describe('parseArg', () => {
  it('turns text into the value a parameter type expects, leaving bad input for the server to reject', () => {
    expect(parseArg('integer', ' 42 ')).toBe(42)
    expect(parseArg('integer', '4x')).toBe('4x')
    expect(parseArg('boolean', 'true')).toBe(true)
    expect(parseArg('boolean', 'maybe')).toBe('maybe')
    expect(parseArg('string', '007')).toBe('007')
  })
})
