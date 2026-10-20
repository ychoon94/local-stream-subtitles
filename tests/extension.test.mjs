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

test('content subtitle messages render without recursion and survive overlay reattachment', async () => {
  class FakeElement {
    constructor(tagName) {
      this.tagName = tagName;
      this.children = [];
      this.parentElement = null;
      this.attributes = new Map();
      this.style = {
        position: '',
        cssText: '',
        setProperty(name, value) { this[name] = value; }
      };
      this.hidden = false;
      this.textContent = '';
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
      return this.shadowRoot;
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

  const player = new FakeElement('div');
  player.id = 'movie_player';
  player.isDocumentRoot = true;
  const overlayHosts = [];
  const document = {
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
  const sandbox = {
    chrome: { runtime: { onMessage: runtimeListeners, sendMessage: async () => undefined } },
    document,
    location: { href: 'https://www.youtube.com/watch?v=abc' },
    URL,
    window: { addEventListener() {} },
    getComputedStyle: () => ({ position: 'static' }),
    requestAnimationFrame(callback) { callback(); },
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

  assert.doesNotThrow(() => onMessage({
    target: 'content',
    type: 'CAPTURE_STARTED',
    preferences: { showJapanese: true, fontSize: 'medium' }
  }));
  assert.doesNotThrow(() => onMessage({
    target: 'content',
    type: 'SUBTITLE',
    subtitle: { original: 'こんにちは', translated: '<img src=x onerror=alert(1)>' }
  }));

  const firstHost = overlayHosts.at(-1);
  assert.ok(firstHost);
  assert.equal(firstHost.parentElement, player);
  assert.equal(firstHost.shadowRoot.querySelector('.original').textContent, 'こんにちは');
  assert.equal(firstHost.shadowRoot.querySelector('.translated').textContent, '<img src=x onerror=alert(1)>');
  assert.ok(firstHost.shadowRoot.querySelector('.stack').classList.contains('visible'));

  firstHost.remove();
  assert.doesNotThrow(() => intervals[1]());
  const replacementHost = overlayHosts.at(-1);
  assert.equal(replacementHost.parentElement, player);
  assert.equal(replacementHost.shadowRoot.querySelector('.translated').textContent, '<img src=x onerror=alert(1)>');
});
