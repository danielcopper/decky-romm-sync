---
paths:
  - "src/**/*.test.ts"
  - "src/**/*.test.tsx"
  - "src/test-utils/**"
---

# Frontend component tests — `@decky/api` event harness

Run with `mise run test:frontend` (Vitest + happy-dom); `mise run test:frontend:coverage` for coverage.

`src/test-utils/decky-api-mock.ts` exposes an in-memory event bus that `addEventListener` / `removeEventListener` route
through. Tests dispatch backend events via `emitDeckyEvent` instead of mocking `@decky/api` per file;
`src/components/CustomPlayButton.test.tsx` is the reference shape. The bus resets between tests; use
`deckyEventListenerCount(name)` to assert `useEffect` cleanup ran. DOM-level `globalThis.dispatchEvent` flows bypass the
harness — happy-dom handles them natively. Prefer the harness over extracting listener bodies into `src/utils/*.ts`
purely for testability.

**Catch coverage assertions must be non-vacuous.** A test claiming `.catch` coverage MUST assert the post-catch state —
the fallback return value, the toast body, the `debugLog` message, the surfaced status. Asserting only that the
rejecting call was invoked is vacuous: it passes with or without the `.catch`.
