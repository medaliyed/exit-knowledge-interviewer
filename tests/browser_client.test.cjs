// Pure client-logic checks using Node's built-in runner; no browser or packages.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {runInNewContext} = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = readFileSync(join(__dirname, '../src/eki/web/static/app.js'), 'utf8');

function client(fetch) {
  const nodes = new Map();
  const document = {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {dataset: {}, textContent: '', addEventListener() {}});
    return nodes.get(id);
  }};
  const api = runInNewContext(source + '\n;({state, deliveryId, stream})', {
    document, crypto: webcrypto, fetch, TextDecoder, Uint8Array,
  });
  api.state.token = 'synthetic-test-token';
  return api;
}
function sse(chunks) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({start(controller) {
    for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
    controller.close();
  }}), {headers: {'content-type': 'text/event-stream', 'X-EKI-Message-ID': 'saved-id'}});
}

test('transport retries keep the ID, while a changed logical payload gets a new ID', () => {
  const identify = client().deliveryId();
  const body = {action: 'correct', corrects: 2, text: 'Restart once.'};
  const first = identify(body);
  assert.equal(identify({...body}), first);
  body.text = 'Restart twice.';
  const changed = identify(body);
  assert.notEqual(changed, first);
  assert.equal(identify({...body}), changed);
  assert.notEqual(identify({...body, corrects: 4}), changed);
});

test('fragmented SSE and keepalive comments preserve saved and terminal delivery', async () => {
  let saved = null;
  const api = client(async (path, options) => {
    assert.equal(options.headers.Authorization, 'Bearer synthetic-test-token');
    return sse([': ping\r\n\r\n', 'event: delivery\r\ndata: {"type":"del',
      'ivery","status":"saved"}\r\n\r\n',
      'event: question\r\ndata: {"type":"question","turn":3}\r', '\n\r\n']);
  });
  await api.stream('/sessions/test/messages', {text: 'Synthetic answer'}, id => { saved = id; });
  assert.equal(saved, 'saved-id');
});

test('a connection ending before a terminal event does not claim completion', async () => {
  const api = client(async () => sse(['data: {"type":"delivery","status":"saved"}\n\n']));
  await assert.rejects(api.stream('/sessions/test/resume'), /connection ended/);
});

test('a retryable provider event remains an error instead of successful delivery', async () => {
  const api = client(async () => sse(['data: {"type":"error","message":"Resume the saved message."}\n\n']));
  await assert.rejects(api.stream('/sessions/test/resume'), /Resume the saved message/);
});
