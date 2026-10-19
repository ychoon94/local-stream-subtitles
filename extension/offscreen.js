const LOCAL_SOCKET_URL = 'ws://127.0.0.1:8765/listen';
const OUTPUT_RATE = 16000;
const FRAME_SAMPLES = 2048;
const MAX_BUFFERED_BYTES = 64 * 1024;
const RESUME_BUFFERED_BYTES = 16 * 1024;
const READY_TIMEOUT_MS = 15000;
const BACKGROUND_URL = chrome.runtime.getURL('background.js');

let activeSession = null;
const cancelledSessions = new Set();

function markSessionCancelled(sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) return;
  cancelledSessions.add(sessionId);
  if (cancelledSessions.size > 32) cancelledSessions.delete(cancelledSessions.values().next().value);
}

function postBackground(message) {
  chrome.runtime.sendMessage({ target: 'background', ...message }).catch(() => {});
}

function cleanMessage(value, fallback = 'Local subtitle service error.') {
  if (typeof value !== 'string') return fallback;
  return value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 240) || fallback;
}

function makeFrame(samples) {
  const buffer = new ArrayBuffer(FRAME_SAMPLES * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < FRAME_SAMPLES; i += 1) view.setInt16(i * 2, samples[i], true);
  return buffer;
}

async function stopCurrent(sessionId = null) {
  const record = activeSession;
  if (!record || (sessionId && record.sessionId !== sessionId)) return false;
  activeSession = null;
  record.intentionalStop = true;
  if (record.readyTimer) clearTimeout(record.readyTimer);
  try { record.socket?.close(1000, 'capture stopped'); } catch { /* already closed */ }
  try { record.source?.disconnect(); } catch { /* already disconnected */ }
  try { record.worklet?.port.postMessage({ type: 'stop' }); } catch { /* worklet already closed */ }
  try { record.worklet?.disconnect(); } catch { /* already disconnected */ }
  for (const track of record.stream?.getTracks() || []) track.stop();
  if (record.context && record.context.state !== 'closed') {
    try { await record.context.close(); } catch { /* context already closed */ }
  }
  record.rejectStart?.(new Error('Capture was stopped before the local service became ready.'));
  return true;
}

function reportUnexpectedEnd(record, message, fatal = true) {
  if (activeSession !== record || record.intentionalStop) return;
  const wasReady = record.ready;
  const errorMessage = cleanMessage(message, 'Audio capture ended unexpectedly.');
  stopCurrent(record.sessionId).finally(() => {
    if (wasReady) {
      postBackground({
        type: 'OFFSCREEN_ENDED',
        sessionId: record.sessionId,
        fatal,
        message: errorMessage
      });
    }
  });
}

function sendStatus(record, state, message, profile) {
  if (activeSession !== record) return;
  record.status = { state, message: cleanMessage(message, 'Connecting to local service…'), mode: record.mode, profile };
  postBackground({ type: 'OFFSCREEN_STATUS', sessionId: record.sessionId, ...record.status });
}

function addPcmSamples(record, samples) {
  if (activeSession !== record || !record.ready || record.socket?.readyState !== WebSocket.OPEN) return;
  for (let i = 0; i < samples.length; i += 1) {
    const sample = samples[i];
    record.frame[record.frameLength] = LocalAudioUtils.int16Sample(sample);
    record.frameLength += 1;
    if (record.frameLength !== FRAME_SAMPLES) continue;

    if (record.socket.bufferedAmount > MAX_BUFFERED_BYTES) {
      if (!record.backpressured) {
        record.backpressured = true;
        sendStatus(record, 'backpressure', 'Local service is behind · dropping audio to keep subtitles live');
      }
    } else {
      try {
        record.socket.send(makeFrame(record.frame));
      } catch {
        reportUnexpectedEnd(record, 'Could not send audio to the local subtitle service.');
        return;
      }
      if (record.backpressured && record.socket.bufferedAmount < RESUME_BUFFERED_BYTES) {
        record.backpressured = false;
        sendStatus(record, 'listening', 'Listening · subtitles will appear on the video');
      }
    }
    record.frameLength = 0;
  }
}

function handleAudioChunk(record, message) {
  if (activeSession !== record || !Array.isArray(message.channels)) return;
  const channels = message.channels
    .filter(buffer => buffer instanceof ArrayBuffer)
    .map(buffer => new Float32Array(buffer));
  if (!channels.length) return;
  const monoAtTargetRate = record.resample(channels);
  if (record.ready) addPcmSamples(record, monoAtTargetRate);
}

function handleServerMessage(record, data) {
  let message;
  try {
    message = JSON.parse(data);
  } catch {
    reportUnexpectedEnd(record, 'Local service sent an invalid response.');
    return;
  }
  if (!message || typeof message !== 'object') return;

  if (message.type === 'ready') {
    if (record.ready || activeSession !== record) return;
    record.ready = true;
    if (record.readyTimer) clearTimeout(record.readyTimer);
    const profile = typeof message.profile === 'string' ? cleanMessage(message.profile, '') : '';
    sendStatus(record, 'listening', profile ? `Listening · ${profile}` : 'Listening · subtitles will appear on the video', profile);
    record.resolveStart({ ok: true });
    return;
  }

  if (message.type === 'status') {
    sendStatus(record, record.ready ? 'listening' : 'connecting', cleanMessage(message.message, record.ready ? 'Listening…' : 'Preparing local speech recognition…'));
    return;
  }

  if (message.type === 'subtitle') {
    if (!record.ready) return;
    const subtitleId = typeof message.id === 'string' || Number.isFinite(message.id)
      ? String(message.id).slice(0, 80)
      : '';
    postBackground({
      type: 'OFFSCREEN_SUBTITLE',
      sessionId: record.sessionId,
      original: typeof message.original === 'string' ? message.original.slice(0, 1200) : '',
      translated: typeof message.translated === 'string' ? message.translated.slice(0, 1200) : '',
      latency_ms: Number.isFinite(message.latency_ms) ? message.latency_ms : null,
      id: subtitleId
    });
    return;
  }

  if (message.type === 'error') {
    const errorMessage = cleanMessage(message.message, 'Local subtitle service error.');
    if (message.fatal === true) {
      if (!record.ready) {
        record.rejectStart?.(new Error(errorMessage));
      } else {
        postBackground({ type: 'OFFSCREEN_ERROR', sessionId: record.sessionId, fatal: true, message: errorMessage });
        stopCurrent(record.sessionId);
      }
    } else {
      postBackground({ type: 'OFFSCREEN_ERROR', sessionId: record.sessionId, fatal: false, message: errorMessage });
    }
  }
}

function openSocket(record, token) {
  return new Promise((resolve, reject) => {
    record.resolveStart = resolve;
    record.rejectStart = reject;
    record.readyTimer = setTimeout(() => reject(new Error('Local service did not become ready. Check the service and try again.')), READY_TIMEOUT_MS);
    const socket = new WebSocket(LOCAL_SOCKET_URL);
    record.socket = socket;
    socket.binaryType = 'arraybuffer';
    socket.addEventListener('open', () => {
      if (activeSession !== record) return;
      try {
        socket.send(JSON.stringify({ type: 'start', token, mode: record.mode }));
        sendStatus(record, 'connecting', 'Connecting to local speech recognition…');
      } catch {
        reject(new Error('Could not start the local subtitle connection.'));
      }
    });
    socket.addEventListener('message', event => {
      if (activeSession === record && typeof event.data === 'string') handleServerMessage(record, event.data);
    });
    socket.addEventListener('error', () => {
      if (!record.ready) sendStatus(record, 'connecting', 'Waiting for the local service…');
    });
    socket.addEventListener('close', event => {
      if (record.intentionalStop || activeSession !== record) return;
      const reason = event.reason ? cleanMessage(event.reason) : 'Local service disconnected.';
      if (!record.ready) reject(new Error(reason));
      else reportUnexpectedEnd(record, reason);
    });
  });
}

async function startCapture(message) {
  if (cancelledSessions.has(message?.sessionId)) return { ok: false, error: 'Capture was cancelled.' };
  if (activeSession) {
    if (activeSession.sessionId === message.sessionId && activeSession.ready) return { ok: true };
    throw new Error('An audio capture session is already active.');
  }
  if (!message || typeof message.sessionId !== 'string' || typeof message.streamId !== 'string' || typeof message.token !== 'string') {
    throw new Error('Invalid capture request.');
  }

  const record = {
    sessionId: message.sessionId,
    tabId: Number(message.tabId),
    mode: message.mode === 'direct' ? 'direct' : 'context',
    ready: false,
    status: { state: 'starting', message: 'Preparing tab audio…', mode: message.mode },
    frame: new Int16Array(FRAME_SAMPLES),
    frameLength: 0,
    intentionalStop: false,
    backpressured: false
  };
  activeSession = record;

  try {
    const constraints = {
      audio: {
        mandatory: {
          chromeMediaSource: 'tab',
          chromeMediaSourceId: message.streamId
        }
      },
      video: false
    };
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    if (activeSession !== record || cancelledSessions.has(record.sessionId)) {
      for (const track of stream.getTracks()) track.stop();
      return { ok: false, error: 'Capture was cancelled.' };
    }
    record.stream = stream;
    const audioTrack = stream.getAudioTracks()[0];
    if (!audioTrack) throw new Error('The YouTube tab did not provide an audio track.');
    audioTrack.addEventListener('ended', () => reportUnexpectedEnd(record, 'Tab audio ended.'), { once: true });

    const context = new AudioContext({ latencyHint: 'interactive' });
    record.context = context;
    record.resample = LocalAudioUtils.createResampler(context.sampleRate, OUTPUT_RATE);
    await context.audioWorklet.addModule(chrome.runtime.getURL('audio-processor.js'));
    if (activeSession !== record || cancelledSessions.has(record.sessionId)) {
      for (const track of stream.getTracks()) track.stop();
      await context.close();
      return { ok: false, error: 'Capture was cancelled.' };
    }

    const source = context.createMediaStreamSource(stream);
    const worklet = new AudioWorkletNode(context, 'tab-audio-pump', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [2],
      channelCount: 2,
      channelCountMode: 'max',
      channelInterpretation: 'speakers'
    });
    record.source = source;
    record.worklet = worklet;
    worklet.port.onmessage = event => handleAudioChunk(record, event.data);
    source.connect(worklet);
    worklet.connect(context.destination);
    await context.resume();
    if (activeSession !== record || cancelledSessions.has(record.sessionId)) return { ok: false, error: 'Capture was cancelled.' };

    await openSocket(record, message.token);
    if (activeSession !== record) return { ok: false, error: 'Capture was cancelled.' };
    return { ok: true };
  } catch (error) {
    await stopCurrent(record.sessionId);
    throw new Error(cleanMessage(error?.message, 'Could not capture tab audio.'));
  }
}

async function getState() {
  if (!activeSession) return { session: null, status: { state: 'idle', message: 'Ready when you are' } };
  return {
    session: {
      sessionId: activeSession.sessionId,
      tabId: activeSession.tabId,
      phase: activeSession.ready ? 'active' : 'starting',
      mode: activeSession.mode
    },
    status: activeSession.status
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== 'offscreen') return false;
  if (sender.id !== chrome.runtime.id || sender.url !== BACKGROUND_URL) {
    sendResponse({ ok: false, error: 'Untrusted capture request.' });
    return false;
  }
  if (message.type === 'START_CAPTURE') {
    startCapture(message).then(sendResponse).catch(error => sendResponse({ ok: false, error: cleanMessage(error?.message, 'Could not start capture.') }));
    return true;
  }
  if (message.type === 'STOP_CAPTURE') {
    markSessionCancelled(message.sessionId);
    stopCurrent(message.sessionId).then(stopped => sendResponse({ ok: true, stopped })).catch(error => sendResponse({ ok: false, error: cleanMessage(error?.message) }));
    return true;
  }
  if (message.type === 'GET_STATE') {
    getState().then(sendResponse).catch(() => sendResponse({ session: null }));
    return true;
  }
  return false;
});
