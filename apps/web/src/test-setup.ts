import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())

if (typeof window !== 'undefined') {
// React Flow measures nodes with ResizeObserver and DOMMatrixReadOnly, which jsdom lacks.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver

class DOMMatrixReadOnlyStub {
  m22: number
  constructor(transform?: string) {
    const scale = transform?.match(/scale\(([1-9.]+)\)/)?.[1]
    this.m22 = scale ? Number(scale) : 1
  }
}
;(globalThis as unknown as { DOMMatrixReadOnly: unknown }).DOMMatrixReadOnly = DOMMatrixReadOnlyStub

Object.defineProperties(globalThis.HTMLElement.prototype, {
  offsetHeight: { get() { return Number.parseFloat(this.style?.height) || 40 } },
  offsetWidth: { get() { return Number.parseFloat(this.style?.width) || 160 } },
})
;(globalThis.SVGElement as unknown as { prototype: { getBBox: () => unknown } }).prototype.getBBox = () => ({
  x: 0, y: 0, width: 0, height: 0,
})
}
