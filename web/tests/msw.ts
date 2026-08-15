import { setupServer } from "msw/node"

// No default handlers on purpose. Every test says what the server answers for
// the routes it exercises, and `setup.ts` starts this with
// `onUnhandledRequest: "error"`, so a request nobody stubbed is a loud failure
// rather than a silent default that quietly decides what the interface renders.
export const server = setupServer()
