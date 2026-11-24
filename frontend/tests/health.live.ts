import assert from 'node:assert/strict'
import { getHealth } from '../src/api/health.ts'

const expected = process.argv[2]
assert.ok(expected === 'connected' || expected === 'unavailable',
  'Specify connected or unavailable')
const request: typeof fetch = (path, options) =>
  fetch(new URL(String(path), 'http://127.0.0.1:8000'), options)
const state = await getHealth(request)
console.log(JSON.stringify(state))
assert.equal(state.kind, expected)
