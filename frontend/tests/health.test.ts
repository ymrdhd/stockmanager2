import assert from 'node:assert/strict'
import { test } from 'node:test'
import { getHealth, type HealthState } from '../src/api/health.ts'

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status })

const request = (result: Response): typeof fetch => async () => result

const unavailable: HealthState = { kind: 'unavailable' }

test('reports a valid health response', async () => {
  assert.deepEqual(await getHealth(request(response({ status: 'ok', version: '2.0.0-dev' }))), {
    kind: 'connected', version: '2.0.0-dev',
  })
})

test('reports network failure', async () => {
  const failing: typeof fetch = async () => { throw new TypeError('offline') }
  assert.deepEqual(await getHealth(failing), unavailable)
})

test('rejects unsuccessful HTTP and invalid health responses', async () => {
  for (const result of [response({}, 503), response({ status: 'ok' }), response({ status: 'ok', version: ' ' }),
    response({ status: 'unknown', version: '2.0' }), new Response('not json')]) {
    assert.deepEqual(await getHealth(request(result)), unavailable)
  }
})
