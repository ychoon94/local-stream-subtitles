import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const { createResampler, int16Sample } = require(resolve(root, 'extension/audio-utils.js'));

test('manifest limits capture access to YouTube, Vimeo, and the fixed local service', async () => {
  const manifest = JSON.parse(await readFile(resolve(root, 'extension/manifest.json'), 'utf8'));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.minimum_chrome_version, '116');
  assert.deepEqual(manifest.host_permissions, [
    'https://www.youtube.com/*',
    'https://youtube.com/*',
    'https://vimeo.com/*',
    'https://www.vimeo.com/*',
    'https://player.vimeo.com/*',
    'http://127.0.0.1:8765/*'
  ]);
  assert.ok(manifest.permissions.includes('tabCapture'));
  assert.ok(!manifest.permissions.includes('microphone'));
  const entryPoints = [
    manifest.background.service_worker,
    manifest.action.default_popup,
    ...manifest.content_scripts.flatMap(script => script.js),
    'offscreen.html',
    'offscreen.js',
    'audio-utils.js',
    'audio-processor.js'
  ];
  for (const entryPoint of entryPoints) {
    await assert.doesNotReject(readFile(resolve(root, 'extension', entryPoint)));
  }
});

test('resampler converts 48 kHz stereo chunks to 16 kHz mono without losing chunk continuity', () => {
  const resample = createResampler(48000, 16000);
  const allOutput = [];
  const totalInput = 48000;
  const left = new Float32Array(totalInput);
  const right = new Float32Array(totalInput);
  for (let i = 0; i < totalInput; i += 1) {
    const sample = Math.sin((2 * Math.PI * 440 * i) / 48000) * 0.5;
    left[i] = sample;
    right[i] = sample;
  }

  for (let offset = 0; offset < totalInput; offset += 2048) {
    const end = Math.min(offset + 2048, totalInput);
    allOutput.push(...resample([left.slice(offset, end), right.slice(offset, end)]));
  }

  assert.ok(Math.abs(allOutput.length - 16000) <= 1, `expected about 16000 samples, got ${allOutput.length}`);
  assert.ok(allOutput.every(Number.isFinite));
  assert.ok(Math.max(...allOutput.slice(0, 1000)) <= 0.501);
  assert.ok(Math.min(...allOutput.slice(0, 1000)) >= -0.501);
});

test('resampler averages stereo channels and PCM conversion clips to signed 16 bit', () => {
  const resample = createResampler(16000, 16000);
  const averaged = resample([
    new Float32Array([1, 1, -1, 0.5, 0]),
    new Float32Array([-1, -1, 1, 0.5, 0])
  ]);

  assert.deepEqual([...averaged], [0, 0, 0, 0.5]);
  assert.equal(int16Sample(-1.5), -32768);
  assert.equal(int16Sample(1.5), 32767);
  assert.equal(int16Sample(0.5), 16384);
});

function makeEvent() {
  const listeners = [];
  return {
    addListener(listener) { listeners.push(listener); },
    listeners
  };
}

function makeWorkerHarness({ initialData = {}, offscreenState = null, hasOffscreen = false, fetchHealth, tabUrl = 'https://www.youtube.com/watch?v=abc' } = {}) {
  const data = new Map(Object.entries(initialData));
  const events = {
    runtimeMessage: makeEvent(),
    startup: makeEvent(),
    tabsRemoved: makeEvent(),
    tabsUpdated: makeEvent(),
    captureStatus: makeEvent()
  };
  let tabGetCalls = 0;
  let captureRequests = 0;
  let offscreenStarts = 0;
  let offscreenCloses = 0;
  const offscreenMessages = [];
  let offscreenPresent = hasOffscreen;
  const contentMessages = [];
  const contentDeliveries = [];
  const chrome = {
    runtime: {
      id: 'test-extension',
      getURL: path => `chrome-extension://test-extension/${path}`,
      onMessage: events.runtimeMessage,
      onStartup: events.startup,
      async sendMessage(message) {
        if (message?.target !== 'offscreen') return undefined;
        if (message.type === 'GET_STATE') return typeof offscreenState === 'function' ? offscreenState() : offscreenState;
        if (message.type === 'START_CAPTURE') { offscreenStarts += 1; return { ok: true }; }
        if (message.type === 'STOP_CAPTURE') return { ok: true };
        if (message.type === 'REPORT_RENDER') { offscreenMessages.push(message); return { ok: true }; }
        if (message.type === 'GET_METRICS') return { observability: false, events: [], counters: {}, benchmarkComplete: false };
        return undefined;
      },
      getContexts: async () => offscreenPresent ? [{ documentUrl: 'chrome-extension://test-extension/offscreen.html' }] : []
    },
    storage: {
      session: {
        async get(keys) {
          return Object.fromEntries(keys.map(key => [key, data.get(key)]).filter(([, value]) => value !== undefined));
        },
        async set(values) { for (const [key, value] of Object.entries(values)) data.set(key, value); },
        async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) data.delete(key); }
      }
    },
    tabs: {
      async get(tabId) { tabGetCalls += 1; return { id: tabId, active: true, url: tabUrl }; },
      sendMessage: async (tabId, message, options) => {
        contentMessages.push(message);
        contentDeliveries.push({ tabId, message, options });
      },
      onRemoved: events.tabsRemoved,
      onUpdated: events.tabsUpdated
    },
    action: { setBadgeText: async () => undefined, setBadgeBackgroundColor: async () => undefined },
    offscreen: {
      async createDocument() { offscreenPresent = true; },
      async closeDocument() { offscreenCloses += 1; offscreenPresent = false; }
    },
    tabCapture: {
      async getMediaStreamId() { captureRequests += 1; return 'stream-id'; },
      onStatusChanged: events.captureStatus
    }
  };
  const sandbox = {
    chrome,
    URL,
    AbortController,
    crypto: globalThis.crypto,
    fetch: fetchHealth || (async () => ({ ok: true, status: 200, json: async () => ({ supported_modes: ['context'] }) })),
    setTimeout,
    clearTimeout,
    Date,
    console,
    Promise,
    Number,
    String,
    Array,
    Object,
    Math
  };
  return {
    chrome,
    data,
    events,
    get tabGetCalls() { return tabGetCalls; },
    get captureRequests() { return captureRequests; },
    get offscreenStarts() { return offscreenStarts; },
    get offscreenCloses() { return offscreenCloses; },
    offscreenMessages,
    contentMessages,
    contentDeliveries,
    async load() {
      const source = await readFile(resolve(root, 'extension/background.js'), 'utf8');
      vm.runInNewContext(source, sandbox, { filename: 'background.js' });
      return (message, sender = {
        id: 'test-extension',
        url: 'chrome-extension://test-extension/popup.html',
        tab: undefined,
        frameId: 0
      }) => new Promise(resolveResponse => {
        const listener = events.runtimeMessage.listeners[0];
        const keepChannelOpen = listener(message, sender, resolveResponse);
        if (!keepChannelOpen) resolveResponse(undefined);
      });
  }
  };
}

test('Vimeo video pages can start capture while unrelated pages and lookalike hosts cannot', async () => {
  const cases = [
    ['https://vimeo.com/76979871', true],
    ['https://www.vimeo.com/76979871/abc123', true],
    ['https://player.vimeo.com/video/76979871?h=abc123', true],
    ['https://vimeo.com/channels/staffpicks/76979871', true],
    ['https://vimeo.com/groups/shortfilms/videos/76979871', true],
    ['https://vimeo.com/showcase/123/video/76979871', true],
    ['https://vimeo.com/', false],
    ['https://vimeo.com/settings', false],
    ['https://vimeo.com.evil.example/76979871', false],
    ['https://evil.example/video/76979871', false],
    ['http://vimeo.com/76979871', false]
  ];
  for (const [tabUrl, supported] of cases) {
    const harness = makeWorkerHarness({ tabUrl });
    const send = await harness.load();
    const result = await send({ target: 'background', type: 'START_REQUEST', tabId: 17, token: 'test-token' });
    assert.equal(result.ok, supported, tabUrl);
    assert.equal(harness.captureRequests, supported ? 1 : 0, tabUrl);
  }
});

test('service worker rejects start and stop requests from YouTube content scripts', async () => {
  const harness = makeWorkerHarness();
  const send = await harness.load();
  const sender = {
    id: 'test-extension',
    url: 'https://www.youtube.com/watch?v=abc',
    tab: { id: 17 },
    frameId: 0
  };

  const start = await send({ target: 'background', type: 'START_REQUEST', tabId: 17, token: 'test-token' }, sender);
  const stop = await send({ target: 'background', type: 'STOP_REQUEST' }, sender);
  const preferences = await send({ target: 'background', type: 'SET_OVERLAY_PREFERENCES', preferences: { fontSize: 'large' } }, sender);

  assert.equal(start.ok, false);
  assert.equal(stop.ok, false);
  assert.equal(preferences.ok, false);
  assert.equal(harness.tabGetCalls, 0);
  assert.equal(harness.data.has('captureSession'), false);
});

test('late Vimeo player frames receive active preferences and frame navigation stops capture', async () => {
  const tabUrl = 'https://vimeo.com/76979871';
  const harness = makeWorkerHarness({ tabUrl });
  const send = await harness.load();
  await send({ target: 'background', type: 'START_REQUEST', tabId: 17, token: 'test-token', preferences: { fontSize: 'large' } });
  const sender = {
    id: 'test-extension', url: 'https://player.vimeo.com/video/76979871',
    tab: { id: 17, url: tabUrl }, frameId: 3
  };
  const ready = await send({ target: 'background', type: 'CONTENT_READY' }, sender);
  assert.equal(ready.ok, true);
  const delivery = harness.contentDeliveries.at(-1);
  assert.equal(delivery.tabId, 17);
  assert.equal(delivery.options.frameId, 3);
  assert.equal(delivery.message.type, 'CAPTURE_STARTED');
  assert.equal(delivery.message.preferences.fontSize, 'large');

  const sessionId = harness.data.get('captureSession').sessionId;
  await send({ target: 'background', type: 'OFFSCREEN_SUBTITLE', sessionId, translated: 'Hello' }, {
    id: 'test-extension', url: 'chrome-extension://test-extension/offscreen.html'
  });
  assert.equal(harness.contentDeliveries.at(-1).message.subtitle.translated, 'Hello');
  assert.equal(harness.contentDeliveries.at(-1).options, undefined, 'Captions reach player frames through a tab broadcast');

  const navigated = await send({ target: 'background', type: 'NAVIGATED', url: 'https://player.vimeo.com/video/123' }, sender);
  assert.equal(navigated.ok, true);
  assert.equal(harness.data.has('captureSession'), false);
  assert.equal(harness.offscreenCloses, 1);
});

test('render acknowledgements require the active trusted tab and session', async () => {
  const harness = makeWorkerHarness();
  const send = await harness.load();
  await send({ target: 'background', type: 'START_REQUEST', tabId: 17, token: 'test-token' });
  const sessionId = harness.data.get('captureSession').sessionId;
  const sender = {
    id: 'test-extension', url: 'https://www.youtube.com/watch?v=abc',
    tab: { id: 17, url: 'https://www.youtube.com/watch?v=abc' }, frameId: 0
  };
  const acknowledgement = {
    target: 'background', type: 'SUBTITLE_RENDERED', sessionId, segment_id: 4,
    first_readable_browser_ms: 1000, final_browser_ms: 1000
  };

  assert.equal((await send(acknowledgement, sender)).ok, true);
  assert.equal(harness.offscreenMessages.length, 1);
  assert.equal(harness.offscreenMessages[0].segment_id, 4);
  assert.equal(harness.offscreenMessages[0].sessionId, sessionId);

  assert.equal((await send({ ...acknowledgement, sessionId: 'stale-session' }, sender)).ignored, true);
  assert.equal((await send(acknowledgement, { ...sender, tab: { ...sender.tab, id: 99 } })).ignored, true);
  assert.equal((await send(acknowledgement, { ...sender, url: 'https://youtube.com.evil.example/watch?v=abc' })).ok, false);
  assert.equal(harness.offscreenMessages.length, 1);
});

test('observability metrics controls accept messages only from the service worker', async () => {
  const harness = makeWorkerHarness();
  const send = await harness.load();
  const command = { target: 'background', type: 'GET_METRICS' };
  assert.equal((await send(command)).error, 'Observability controls are available to the benchmark runner only.');
  const result = await send(command, {
    id: 'test-extension', url: 'chrome-extension://test-extension/background.js', frameId: 0
  });
  assert.equal(result.observability, false);
});

test('untrusted player frames cannot synchronize captions or stop another video session', async () => {
  const tabUrl = 'https://vimeo.com/76979871';
  const harness = makeWorkerHarness({ tabUrl });
  const send = await harness.load();
  await send({ target: 'background', type: 'START_REQUEST', tabId: 17, token: 'test-token' });
  const senders = [
    { url: 'https://player.vimeo.com.evil.example/video/123', tab: { id: 17, url: tabUrl }, frameId: 3 },
    { url: 'https://player.vimeo.com/video/123', tab: { id: 17, url: 'https://evil.example/' }, frameId: 3 },
    { url: 'https://www.youtube.com/watch?v=abc', tab: { id: 17, url: tabUrl }, frameId: 3 },
    { url: 'http://vimeo.com/76979871', tab: { id: 17, url: tabUrl }, frameId: 0 }
  ];
  const deliveriesBefore = harness.contentDeliveries.length;
  for (const sender of senders) {
    for (const type of ['CONTENT_READY', 'NAVIGATED']) {
      const result = await send({ target: 'background', type, url: '' }, { id: 'test-extension', ...sender });
      assert.equal(result.ok, false, `${type} from ${sender.url}`);
    }
  }
  await send({ target: 'background', type: 'CONTENT_READY' }, {
    id: 'test-extension', url: 'https://player.vimeo.com/video/123', tab: { id: 99, url: tabUrl }, frameId: 3
  });
  assert.equal(harness.contentDeliveries.length, deliveriesBefore);
  assert.equal(harness.data.get('captureSession').phase, 'active');
  assert.equal(harness.offscreenCloses, 0);
});

test('a stop during the local health check cancels the start without leaving session state', async () => {
  let resolveHealth;
  let notifyHealth;
  const healthRequested = new Promise(resolve => { notifyHealth = resolve; });
  const harness = makeWorkerHarness({ fetchHealth: () => {
    notifyHealth();
    return new Promise(resolve => { resolveHealth = resolve; });
  } });
  const send = await harness.load();
  const startPromise = send({
    target: 'background', type: 'START_REQUEST', tabId: 17, url: 'https://www.youtube.com/watch?v=abc', token: 'test-token'
  });

  await healthRequested;
  await send({ target: 'background', type: 'GET_STATUS' });
  assert.equal(harness.data.get('captureSession').phase, 'starting');
  const stop = await send({ target: 'background', type: 'STOP_REQUEST' });
  resolveHealth({ ok: true, status: 200, json: async () => ({ supported_modes: ['context'] }) });
  const start = await startPromise;

  assert.equal(stop.status.state, 'idle');
  assert.equal(start.cancelled, true);
  assert.equal(harness.data.has('captureSession'), false);
  assert.equal(harness.data.get('captureStatus').state, 'idle');
});

test('fresh worker clears an interrupted start so the next request performs capture', async () => {
  const oldSession = {
    sessionId: 'interrupted-session',
    tabId: 17,
    url: 'https://www.youtube.com/watch?v=abc',
    phase: 'starting',
    preferences: { mode: 'context', showJapanese: true, fontSize: 'medium' },
    startedAt: 1
  };
  const harness = makeWorkerHarness({ initialData: {
    captureSession: oldSession,
    captureStatus: { state: 'starting', message: 'Preparing tab audio…', mode: 'context' }
  } });
  const send = await harness.load();

  const recovered = await send({ target: 'background', type: 'GET_STATUS' });
  assert.equal(recovered.status.state, 'error');
  assert.match(recovered.status.message, /start again/i);
  assert.equal(harness.data.has('captureSession'), false);

  const start = await send({
    target: 'background',
    type: 'START_REQUEST',
    tabId: 17,
    url: oldSession.url,
    token: 'test-token',
    preferences: oldSession.preferences
  });
  assert.equal(start.ok, true);
  assert.equal(harness.captureRequests, 1);
  assert.equal(harness.offscreenStarts, 1);
  assert.equal(harness.data.get('captureSession').phase, 'active');
});

test('fresh worker restores a ready matching offscreen capture and the overlay', async () => {
  const session = {
    sessionId: 'ready-session',
    tabId: 17,
    url: 'https://www.youtube.com/watch?v=abc',
    phase: 'starting',
    preferences: { mode: 'context', showJapanese: true, fontSize: 'large' },
    startedAt: 1
  };
  const harness = makeWorkerHarness({
    initialData: {
      captureSession: session,
      captureStatus: { state: 'starting', message: 'Preparing tab audio…', mode: 'context' }
    },
    hasOffscreen: true,
    offscreenState: {
      session: { sessionId: session.sessionId, tabId: session.tabId, phase: 'active', mode: 'context' },
      status: { state: 'listening', message: 'Listening · test profile', mode: 'context', profile: 'test profile' }
    }
  });
  const send = await harness.load();

  const recovered = await send({ target: 'background', type: 'GET_STATUS' });
  assert.equal(recovered.status.state, 'listening');
  assert.equal(harness.data.get('captureSession').phase, 'active');
  assert.equal(harness.captureRequests, 0);
  assert.equal(harness.offscreenStarts, 0);
  assert.ok(harness.contentMessages.some(message => message.type === 'CAPTURE_STARTED'));
});

test('delayed stale recovery cannot close a replacement capture after stop and restart', async () => {
  const staleSession = {
    sessionId: 'stale-session',
    tabId: 17,
    url: 'https://www.youtube.com/watch?v=abc',
    phase: 'starting',
    preferences: { mode: 'context', showJapanese: true, fontSize: 'medium' },
    startedAt: 1
  };
  let resolveOldState;
  let notifyGetState;
  const getStateRequested = new Promise(resolve => { notifyGetState = resolve; });
  const delayedOldState = new Promise(resolve => { resolveOldState = resolve; });
  const harness = makeWorkerHarness({
    initialData: {
      captureSession: staleSession,
      captureStatus: { state: 'starting', message: 'Preparing tab audio…', mode: 'context' }
    },
    hasOffscreen: true,
    offscreenState: () => {
      notifyGetState();
      return delayedOldState;
    }
  });
  const send = await harness.load();

  const staleRecovery = send({ target: 'background', type: 'GET_STATUS' });
  await getStateRequested;
  const stopped = await send({ target: 'background', type: 'STOP_REQUEST' });
  assert.equal(stopped.status.state, 'idle');

  const started = await send({
    target: 'background',
    type: 'START_REQUEST',
    tabId: 17,
    url: staleSession.url,
    token: 'test-token',
    preferences: staleSession.preferences
  });
  assert.equal(started.ok, true);
  const replacement = harness.data.get('captureSession');
  assert.notEqual(replacement.sessionId, staleSession.sessionId);
  assert.equal(replacement.phase, 'active');

  resolveOldState({ session: null, status: { state: 'idle', message: 'Ready when you are' } });
  const recovered = await staleRecovery;

  assert.equal(recovered.status.state, 'listening');
  assert.equal(harness.data.get('captureSession').sessionId, replacement.sessionId);
  assert.equal(harness.data.get('captureSession').phase, 'active');
  assert.equal(harness.captureRequests, 1);
  assert.equal(harness.offscreenStarts, 1);
  assert.equal(harness.offscreenCloses, 1, 'only the completed Stop should close the previous offscreen document');
  assert.equal((await harness.chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] })).length, 1);
});

test('offscreen stop tombstone prevents a late capture start from opening the microphone path', async () => {
  const listeners = makeEvent();
  let mediaRequests = 0;
  const chrome = {
    runtime: {
      id: 'test-extension',
      getURL: path => `chrome-extension://test-extension/${path}`,
      onMessage: listeners,
      sendMessage: async () => undefined
    }
  };
  const sandbox = {
    chrome,
    navigator: { mediaDevices: { getUserMedia: async () => { mediaRequests += 1; throw new Error('should not start'); } } },
    WebSocket: class WebSocket {},
    ArrayBuffer,
    DataView,
    Float32Array,
    Int16Array,
    Math,
    Number,
    String,
    Array,
    Object,
    Promise,
    setTimeout,
    clearTimeout
  };
  const source = await readFile(resolve(root, 'extension/offscreen.js'), 'utf8');
  vm.runInNewContext(source, sandbox, { filename: 'offscreen.js' });
  const listener = listeners.listeners[0];
  const trustedSender = { id: 'test-extension', url: 'chrome-extension://test-extension/background.js' };

  const stop = await new Promise(resolveResponse => listener({
    target: 'offscreen', type: 'STOP_CAPTURE', sessionId: 'session-1'
  }, trustedSender, resolveResponse));
  const start = await new Promise(resolveResponse => listener({
    target: 'offscreen', type: 'START_CAPTURE', sessionId: 'session-1', streamId: 'expired-stream', token: 'test-token'
  }, trustedSender, resolveResponse));

  assert.equal(stop.ok, true);
  assert.equal(start.ok, false);
  assert.equal(mediaRequests, 0);
});

test('clock calibration maps browser timestamps into server time with a measured uncertainty', async () => {
  const listeners = makeEvent();
  const sandbox = {
    chrome: { runtime: { id: 'test-extension', getURL: path => `chrome-extension://test-extension/${path}`, onMessage: listeners } },
    performance: { timeOrigin: 0, now: () => 0 },
    Map,
    Set,
    Promise,
    Number,
    Array,
    Object,
    Math,
    setTimeout,
    clearTimeout
  };
  const source = await readFile(resolve(root, 'extension/offscreen.js'), 'utf8');
  const calculate = vm.runInNewContext(`${source}\ncalculateClockCalibration`, sandbox, { filename: 'offscreen.js' });
  const clientSentMs = 1700000000000;
  const calibration = calculate(clientSentMs, clientSentMs - 299990, clientSentMs - 299989, clientSentMs + 22);

  assert.equal(calibration.offsetMs, -300000.5);
  assert.equal(calibration.uncertaintyMs, 10.5);
  const calibratedResolution = calculate(clientSentMs, clientSentMs - 299990, clientSentMs - 299989, clientSentMs + 22, 16.625);
  assert.equal(calibratedResolution.uncertaintyMs, 27.125,
    'Reported server resolution and the browser precision allowance widen the confidence bound.');
  assert.equal(calculate(10, 20, 21, 9), null, 'Impossible negative round trips are discarded.');
  assert.equal(calculate(10, 20, 21, Number.NaN), null, 'Non-finite clock samples are discarded.');
  assert.equal(calculate(10, 20, 21, 22, Number.NaN), null, 'Non-finite clock resolution is discarded.');
  assert.equal(calculate(10, 20, 21, 22, 101), null, 'Implausibly large clock resolution is discarded.');
});

test('production clock calibration includes server resolution and browser timestamp precision', async () => {
  const listeners = makeEvent();
  const sandbox = {
    chrome: { runtime: { id: 'test-extension', getURL: path => `chrome-extension://test-extension/${path}`, onMessage: listeners } },
    performance: { timeOrigin: 1700000000000, now: () => 20 },
    WebSocket: { OPEN: 1 },
    Map,
    Set,
    Promise,
    Number,
    Array,
    Object,
    Math,
    JSON,
    queueMicrotask,
    setTimeout,
    clearTimeout
  };
  const source = await readFile(resolve(root, 'extension/offscreen.js'), 'utf8');
  const api = vm.runInNewContext(
    `${source}\n({ calibrateClock, setActiveSession(value) { activeSession = value; } })`,
    sandbox,
    { filename: 'offscreen.js' }
  );
  const record = {
    observability: true, clockSyncPending: new Map(),
    socket: {
      readyState: 1,
      send(serialized) {
        const request = JSON.parse(serialized);
        queueMicrotask(() => record.clockSyncPending.get(request.client_sent_ms)?.({
          server_received_ms: request.client_sent_ms - 299990,
          server_sent_ms: request.client_sent_ms - 299989,
          clock_resolution_ms: 15.625,
          client_received_ms: request.client_sent_ms + 22
        }));
      }
    }
  };
  api.setActiveSession(record);

  await api.calibrateClock(record);

  assert.equal(record.clockUncertaintyMs, 27.125,
    'Production calibration adds the reply resolution (15.625 ms), browser precision allowance (1 ms), and half the measured network RTT (10.5 ms).');
});

test('benchmark metrics retain supported outcomes and report bounded event loss', async () => {
  const listeners = makeEvent();
  const sandbox = {
    chrome: { runtime: { id: 'test-extension', getURL: path => `chrome-extension://test-extension/${path}`, onMessage: listeners } },
    performance: { timeOrigin: 0, now: () => 0 },
    Map,
    Set,
    Promise,
    Number,
    Array,
    Object,
    Math,
    setTimeout,
    clearTimeout
  };
  const source = await readFile(resolve(root, 'extension/offscreen.js'), 'utf8');
  const api = vm.runInNewContext(
    `${source}\n({ recordMetrics, setActiveSession(value) { activeSession = value; } })`,
    sandbox,
    { filename: 'offscreen.js' }
  );
  const record = { metricEvents: [], lostEvents: 0, metricCounters: {}, benchmarkComplete: false };
  api.recordMetrics(record, {
    type: 'metrics',
    event: { segment_id: 1, outcome: 'emitted', source: 'source_eof', transcript: 'private text', timings: { asr_ms: 7 } },
    counters: { emitted: 1 }
  });
  assert.equal(record.metricEvents[0].outcome, 'emitted');
  assert.equal(record.metricEvents[0].source, 'source_eof');
  assert.equal(Object.hasOwn(record.metricEvents[0], 'transcript'), false);
  api.recordMetrics(record, { type: 'metrics', event: { segment_id: 1, outcome: 'asr_failed', source: 'source_eof' } });
  for (let segmentId = 2; segmentId <= 511; segmentId += 1) {
    api.recordMetrics(record, { type: 'metrics', event: { segment_id: segmentId, outcome: 'empty_asr', source: 'live' } });
  }
  api.recordMetrics(record, { type: 'metrics', event: { segment_id: 513, outcome: 'rendered', source: 'live' } });
  api.setActiveSession(record);

  let result;
  const keepChannelOpen = listeners.listeners[0]({ target: 'offscreen', type: 'GET_METRICS' }, {
    id: 'test-extension', url: 'chrome-extension://test-extension/background.js'
  }, value => { result = value; });

  assert.equal(keepChannelOpen, false);
  assert.equal(result.events.length, 512);
  assert.equal(result.lostEvents, 1);
  assert.equal(result.events[0].segment_id, 1);
  assert.equal(result.events[0].outcome, 'asr_failed');
  assert.equal(result.events[0].source, 'source_eof');
  assert.equal(result.events[1].source, 'live');
  assert.equal(result.events.at(-1).outcome, 'rendered');
  assert.equal(result.counters.emitted, 1);
  assert.equal(Object.hasOwn(result.events[0], 'transcript'), false);
});

test('ending a benchmark flushes pending audio drops before requesting completion', async () => {
  const listeners = makeEvent();
  const sandbox = {
    chrome: { runtime: { id: 'test-extension', getURL: path => `chrome-extension://test-extension/${path}`, onMessage: listeners } },
    performance: { timeOrigin: 0, now: () => 0 },
    Map,
    Set,
    Promise,
    Number,
    Array,
    Object,
    Math,
    JSON,
    WebSocket: { OPEN: 1 },
    setTimeout,
    clearTimeout
  };
  const source = await readFile(resolve(root, 'extension/offscreen.js'), 'utf8');
  const api = vm.runInNewContext(
    `${source}\n({ setActiveSession(value) { activeSession = value; } })`,
    sandbox,
    { filename: 'offscreen.js' }
  );
  const sent = [];
  const record = {
    sessionId: 'session-1', observability: true, droppedFrames: 3,
    socket: { readyState: 1, send(value) { sent.push(JSON.parse(value)); } }
  };
  api.setActiveSession(record);

  let response;
  const keepChannelOpen = listeners.listeners[0]({ target: 'offscreen', type: 'END_BENCHMARK' }, {
    id: 'test-extension', url: 'chrome-extension://test-extension/background.js'
  }, value => { response = value; });

  assert.equal(keepChannelOpen, false);
  assert.deepEqual(sent, [
    { type: 'audio_dropped', frames: 3, samples: 6144 },
    { type: 'benchmark_end' }
  ]);
  assert.equal(record.droppedFrames, 0);
  assert.equal(response.ok, true);
});

test('content subtitle messages render without recursion and survive overlay reattachment', async () => {
  class FakeElement {
    constructor(tagName) {
      this.tagName = tagName;
      this.children = [];
      this.parentElement = null;
      this.attributes = new Map();
      this.style = {
        position: '',
        display: '',
        visibility: '',
        opacity: '',
        contentVisibility: '',
        cssText: '',
        setProperty(name, value) { this[name] = value; }
      };
      this.hidden = false;
      this.textContent = '';
      this.rect = { left: 10, top: 10, width: 100, height: 20 };
      this._className = '';
      this.classList = {
        add: name => this._setClass(name, true),
        remove: name => this._setClass(name, false),
        contains: name => this._className.split(/\s+/).includes(name)
      };
    }
    set className(value) { this._className = value; }
    get className() { return this._className; }
    _setClass(name, enabled) {
      const classes = new Set(this._className.split(/\s+/).filter(Boolean));
      if (enabled) classes.add(name);
      else classes.delete(name);
      this._className = [...classes].join(' ');
    }
    setAttribute(name, value) { this.attributes.set(name, value); }
    append(...elements) { for (const element of elements) this.appendChild(element); }
    appendChild(element) {
      element.remove();
      this.children.push(element);
      element.parentElement = this;
      return element;
    }
    remove() {
      if (!this.parentElement) return;
      this.parentElement.children = this.parentElement.children.filter(child => child !== this);
      this.parentElement = null;
    }
    attachShadow() {
      this.shadowRoot = new FakeElement('shadow-root');
      this.shadowRoot.host = this;
      return this.shadowRoot;
    }
    getRootNode() {
      let node = this;
      while (node.parentElement) node = node.parentElement;
      return node;
    }
    getBoundingClientRect() {
      const { left, top, width, height } = this.rect;
      return { left, top, width, height, right: left + width, bottom: top + height };
    }
    querySelector(selector) {
      if (!selector.startsWith('.')) return null;
      const wanted = selector.slice(1);
      const queue = [...this.children];
      while (queue.length) {
        const element = queue.shift();
        if (element.className.split(/\s+/).includes(wanted)) return element;
        queue.push(...element.children);
      }
      return null;
    }
    get isConnected() {
      let node = this;
      while (node) {
        if (node.isDocumentRoot) return true;
        node = node.parentElement;
      }
      return false;
    }
  }

  const ancestor = new FakeElement('div');
  ancestor.isDocumentRoot = true;
  const player = new FakeElement('div');
  player.id = 'movie_player';
  ancestor.appendChild(player);
  const overlayHosts = [];
  const document = {
    visibilityState: 'visible',
    addEventListener() {},
    querySelector(selector) { return selector === '#movie_player' ? player : null; },
    createElement(tagName) {
      const element = new FakeElement(tagName);
      if (tagName === 'div' && element.attachShadow) {
        const attach = element.attachShadow.bind(element);
        element.attachShadow = (...args) => {
          const shadow = attach(...args);
          overlayHosts.push(element);
          return shadow;
        };
      }
      return element;
    }
  };
  const runtimeListeners = makeEvent();
  const intervals = [];
  const animationFrames = [];
  const runtimeMessages = [];
  let opacity = 0;
  const sandbox = {
    chrome: { runtime: { onMessage: runtimeListeners, sendMessage: async message => runtimeMessages.push(message) } },
    document,
    location: { href: 'https://www.youtube.com/watch?v=abc' },
    URL,
    window: { addEventListener() {}, innerWidth: 1200, innerHeight: 800 },
    getComputedStyle: element => ({
      position: element.style.position || 'static',
      display: element.hidden ? 'none' : (element.style.display || 'block'),
      visibility: element.style.visibility || 'visible',
      opacity: String(element.classList.contains('stack') ? opacity : (element.style.opacity || 1)),
      contentVisibility: element.style.contentVisibility || 'visible'
    }),
    performance: { timeOrigin: 1000000, now: () => 42 },
    requestAnimationFrame(callback) { animationFrames.push(callback); },
    setInterval(callback) { intervals.push(callback); return intervals.length; },
    setTimeout() { return 1; },
    clearTimeout() {},
    Object,
    String,
    Number,
    Array
  };
  const source = await readFile(resolve(root, 'extension/content.js'), 'utf8');
  vm.runInNewContext(source, sandbox, { filename: 'content.js' });
  const onMessage = runtimeListeners.listeners[0];
  const runNextFrame = () => animationFrames.shift()?.();
  const drainFrames = () => {
    let count = 0;
    while (animationFrames.length && count < 200) {
      runNextFrame();
      count += 1;
    }
  };

  assert.doesNotThrow(() => onMessage({
    target: 'content',
    type: 'CAPTURE_STARTED',
    sessionId: 'session-1',
    preferences: { showJapanese: true, fontSize: 'medium' }
  }));
  assert.doesNotThrow(() => onMessage({
    target: 'content',
    type: 'SUBTITLE',
    sessionId: 'stale-session',
    subtitle: { original: '', translated: 'Stale', telemetry: { segment_id: 6 } }
  }));
  assert.equal(overlayHosts[0].shadowRoot.querySelector('.translated').textContent, '');
  assert.doesNotThrow(() => onMessage({
    target: 'content', type: 'SUBTITLE', sessionId: 'session-1',
    subtitle: { original: 'こんにちは', translated: 'Legacy final subtitle' }
  }));
  runNextFrame();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 0,
    'Final-only captions keep rendering when timing metadata is absent.');
  assert.doesNotThrow(() => onMessage({
    target: 'content',
    type: 'SUBTITLE',
    sessionId: 'session-1',
    subtitle: { original: 'こんにちは', translated: '<img src=x onerror=alert(1)>', telemetry: { segment_id: 7 } }
  }));
  runNextFrame();
  runNextFrame();

  const firstHost = overlayHosts.at(-1);
  assert.ok(firstHost);
  assert.equal(firstHost.parentElement, player);
  assert.equal(firstHost.shadowRoot.querySelector('.original').textContent, 'こんにちは');
  assert.equal(firstHost.shadowRoot.querySelector('.translated').textContent, '<img src=x onerror=alert(1)>');
  assert.ok(firstHost.shadowRoot.querySelector('.stack').classList.contains('visible'));
  runNextFrame();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 0,
    'A caption is not reported while its foreground opacity is below the readability threshold.');

  opacity = 1;
  const translated = firstHost.shadowRoot.querySelector('.translated');
  player.style.display = 'none';
  runNextFrame();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 0,
    'A hidden player cannot produce a render acknowledgement.');

  player.style.display = '';
  ancestor.style.visibility = 'hidden';
  runNextFrame();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 0,
    'A hidden CSS ancestor cannot produce a render acknowledgement.');

  ancestor.style.visibility = '';
  ancestor.style.opacity = '0';
  runNextFrame();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 0,
    'An ancestor opacity of zero cannot produce a render acknowledgement.');

  ancestor.style.opacity = '';
  translated.rect = { left: -200, top: 10, width: 100, height: 20 };
  runNextFrame();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 0,
    'A translated caption box fully outside the viewport cannot produce a render acknowledgement.');

  translated.rect = { left: 10, top: 10, width: 100, height: 20 };
  drainFrames();
  const acknowledgements = runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED');
  assert.equal(acknowledgements.length, 1);
  assert.equal(acknowledgements[0].first_readable_browser_ms, 1000042);
  assert.equal(acknowledgements[0].final_browser_ms, 1000042);

  firstHost.remove();
  assert.doesNotThrow(() => intervals[1]());
  const replacementHost = overlayHosts.at(-1);
  assert.equal(replacementHost.parentElement, player);
  assert.equal(replacementHost.shadowRoot.querySelector('.translated').textContent, '<img src=x onerror=alert(1)>');
  drainFrames();
  assert.equal(runtimeMessages.filter(message => message.type === 'SUBTITLE_RENDERED').length, 1,
    'Reattaching the same caption does not emit a second render acknowledgement.');
});
