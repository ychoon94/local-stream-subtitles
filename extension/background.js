const OFFSCREEN_PATH = 'offscreen.html';
const OFFSCREEN_URL = chrome.runtime.getURL(OFFSCREEN_PATH);
const HEALTH_URL = 'http://127.0.0.1:8765/health';
const SESSION_KEY = 'captureSession';
const STATUS_KEY = 'captureStatus';

let creatingOffscreen;
let startingOperation;
let stoppingOperation;
let recoveringStartingOperation;
let startEpoch = 0;
let lifecycleGeneration = 0;

function cleanMessage(value, fallback = 'Local service error.') {
  if (typeof value !== 'string') return fallback;
  const cleaned = value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim();
  return cleaned.slice(0, 240) || fallback;
}

function safePreferences(input = {}) {
  return {
    mode: input.mode === 'direct' ? 'direct' : 'context',
    showJapanese: input.showJapanese !== false,
    fontSize: ['small', 'medium', 'large'].includes(input.fontSize) ? input.fontSize : 'medium'
  };
}

function isSupportedVideo(url) {
  if (typeof url !== 'string') return false;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:') return false;
    if (['youtube.com', 'www.youtube.com'].includes(parsed.hostname)) {
      return parsed.pathname === '/watch' || parsed.pathname.startsWith('/live/');
    }
    if (parsed.hostname === 'player.vimeo.com') return /^\/video\/\d+\/?$/.test(parsed.pathname);
    return ['vimeo.com', 'www.vimeo.com'].includes(parsed.hostname) &&
      /^\/(?:\d+(?:\/[a-zA-Z0-9]+)?|channels\/[^/]+\/\d+|groups\/[^/]+\/videos\/\d+|(?:showcase|album)\/\d+\/video\/\d+)\/?$/.test(parsed.pathname);
  } catch {
    return false;
  }
}

function isTrustedContentSender(sender) {
  if (!sender.tab) return false;
  try {
    const url = new URL(sender.url);
    if (url.protocol !== 'https:') return false;
    if (sender.frameId === 0) {
      return ['youtube.com', 'www.youtube.com', 'vimeo.com', 'www.vimeo.com', 'player.vimeo.com'].includes(url.hostname);
    }
    const tabUrl = new URL(sender.tab.url);
    return url.hostname === 'player.vimeo.com' && isSupportedVideo(sender.url) &&
      ['vimeo.com', 'www.vimeo.com', 'player.vimeo.com'].includes(tabUrl.hostname) && isSupportedVideo(sender.tab.url);
  } catch {
    return false;
  }
}

async function getSession() {
  const result = await chrome.storage.session.get([SESSION_KEY, STATUS_KEY]);
  return {
    session: result[SESSION_KEY] || null,
    status: result[STATUS_KEY] || { state: 'idle', message: 'Ready when you are' }
  };
}

async function writeSession(session) {
  if (session) await chrome.storage.session.set({ [SESSION_KEY]: session });
  else await chrome.storage.session.remove(SESSION_KEY);
}

async function writeStatus(status) {
  const normalized = {
    state: status.state,
    message: cleanMessage(status.message, 'Ready when you are'),
    mode: status.mode === 'direct' ? 'direct' : status.mode === 'context' ? 'context' : undefined,
    profile: typeof status.profile === 'string' ? cleanMessage(status.profile, '').slice(0, 60) : undefined,
    updatedAt: Date.now()
  };
  await chrome.storage.session.set({ [STATUS_KEY]: normalized });
  await updateBadge(normalized.state);
  chrome.runtime.sendMessage({ target: 'popup', type: 'STATUS_UPDATE', status: normalized }).catch(() => {});
  return normalized;
}

async function updateBadge(state) {
  const text = state === 'listening' || state === 'backpressure' ? 'ON' :
    ['checking', 'starting', 'connecting'].includes(state) ? '…' : '';
  await chrome.action.setBadgeText({ text });
  if (text) await chrome.action.setBadgeBackgroundColor({ color: state === 'backpressure' ? '#b9822c' : '#5e9137' });
}

async function hasOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ['OFFSCREEN_DOCUMENT'],
    documentUrls: [OFFSCREEN_URL]
  });
  return contexts.length > 0;
}

async function ensureOffscreenDocument() {
  if (await hasOffscreenDocument()) return;
  if (!creatingOffscreen) {
    creatingOffscreen = chrome.offscreen.createDocument({
      url: OFFSCREEN_PATH,
      reasons: ['USER_MEDIA', 'AUDIO_PLAYBACK'],
      justification: 'Capture the current video tab audio, keep playback audible, and stream local audio frames for subtitle recognition.'
    }).finally(() => {
      creatingOffscreen = null;
    });
  }
  await creatingOffscreen;
}

async function sendToOffscreen(message) {
  return chrome.runtime.sendMessage({ ...message, target: 'offscreen' });
}

async function closeOffscreenDocument() {
  try {
    if (await hasOffscreenDocument()) await chrome.offscreen.closeDocument();
  } catch {
    // The document may have closed after its final audio track ended.
  }
}

async function checkLocalHealth(mode) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2500);
  let response;
  try {
    response = await fetch(HEALTH_URL, { cache: 'no-store', signal: controller.signal });
  } catch (error) {
    throw new Error(error?.name === 'AbortError'
      ? 'Local service did not respond. Start the local subtitle service and try again.'
      : 'Local service is unavailable. Start the local subtitle service and try again.');
  } finally {
    clearTimeout(timeout);
  }
  if (!response.ok) throw new Error(`Local service returned HTTP ${response.status}.`);
  const health = await response.json().catch(() => null);
  if (mode === 'direct' && (!Array.isArray(health?.supported_modes) || !health.supported_modes.includes('direct'))) {
    throw new Error('Direct translation is unavailable for the installed speech model. Choose Context mode.');
  }
}

async function isCurrentStartingSession(sessionId) {
  const { session } = await getSession();
  return session?.sessionId === sessionId && session.phase === 'starting';
}

function wasStartCancelled(epoch) {
  return epoch !== startEpoch;
}

async function cancelledStartResult() {
  const { status } = await getSession();
  await closeOffscreenDocument();
  return { ok: false, cancelled: true, status };
}

async function startSessionInternal(message, epoch) {
  const tabId = Number(message.tabId);
  const token = typeof message.token === 'string' ? message.token.trim() : '';
  if (!Number.isInteger(tabId) || tabId < 0) throw new Error('Select a YouTube or Vimeo video tab first.');
  if (!token || token.length > 512) throw new Error('Enter a valid local pairing token.');

  const tab = await chrome.tabs.get(tabId);
  if (wasStartCancelled(epoch)) return cancelledStartResult();
  if (!tab.active || !isSupportedVideo(tab.url)) throw new Error('Open a YouTube video, live stream, or Vimeo video in the active tab.');

  const { session: existing } = await getSession();
  if (wasStartCancelled(epoch)) return cancelledStartResult();
  if (existing && existing.phase !== 'idle') {
    if (existing.tabId === tabId && ['active', 'starting'].includes(existing.phase)) {
      const { status } = await getSession();
      return { ok: true, status };
    }
    throw new Error('Subtitles are already running in another tab. Stop that session first.');
  }

  const preferences = safePreferences(message.preferences);
  const session = {
    sessionId: crypto.randomUUID(),
    tabId,
    url: tab.url,
    phase: 'starting',
    preferences,
    startedAt: Date.now()
  };
  await writeSession(session);
  if (wasStartCancelled(epoch)) {
    const latest = await getSession();
    if (latest.session?.sessionId === session.sessionId) await writeSession(null);
    return cancelledStartResult();
  }
  await writeStatus({ state: 'checking', message: 'Checking the local subtitle service…', mode: preferences.mode });
  if (wasStartCancelled(epoch)) return cancelledStartResult();

  try {
    await checkLocalHealth(preferences.mode);
    if (wasStartCancelled(epoch) || !await isCurrentStartingSession(session.sessionId)) return cancelledStartResult();

    await writeStatus({ state: 'starting', message: 'Preparing tab audio…', mode: preferences.mode });
    await ensureOffscreenDocument();
    if (wasStartCancelled(epoch) || !await isCurrentStartingSession(session.sessionId)) return cancelledStartResult();

    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });
    if (wasStartCancelled(epoch) || !await isCurrentStartingSession(session.sessionId)) return cancelledStartResult();

    const result = await sendToOffscreen({
      type: 'START_CAPTURE',
      sessionId: session.sessionId,
      tabId,
      streamId,
      token,
      mode: preferences.mode
    });
    if (!result?.ok) throw new Error(result?.error || 'Could not start tab audio capture.');

    const latest = await getSession();
    if (latest.session?.sessionId !== session.sessionId || latest.session.phase !== 'starting') {
      await sendToOffscreen({ type: 'STOP_CAPTURE', sessionId: session.sessionId, reason: 'cancelled' }).catch(() => {});
      return cancelledStartResult();
    }

    await writeSession({ ...session, phase: 'active' });
    const status = await writeStatus({ state: 'listening', message: 'Listening · subtitles will appear on the video', mode: preferences.mode });
    await sendToContent(tabId, { type: 'CAPTURE_STARTED', preferences });
    return { ok: true, status };
  } catch (error) {
    const latest = await getSession();
    if (wasStartCancelled(epoch)) return cancelledStartResult();
    if (latest.session?.sessionId === session.sessionId && latest.session.phase !== 'stopping') {
      await writeSession(null);
      await closeOffscreenDocument();
      const status = await writeStatus({ state: 'error', message: cleanMessage(error?.message, 'Could not start subtitles.'), mode: preferences.mode });
      await sendToContent(tabId, { type: 'CAPTURE_STOPPED' });
      return { ok: false, error: status.message, status };
    }
    return { ok: false, error: 'Start was cancelled.' };
  }
}

async function startSession(message) {
  const requestedTabId = Number(message.tabId);
  if (startingOperation) {
    if (startingOperation.tabId === requestedTabId) return startingOperation.promise;
    throw new Error('Subtitles are already starting in another tab.');
  }
  const epoch = startEpoch;
  const operation = { tabId: requestedTabId };
  operation.promise = (async () => {
    const { session } = await getSession();
    if (session?.phase === 'starting') await reconcileStatus({ recoverStaleStart: true });
    if (wasStartCancelled(epoch)) return cancelledStartResult();
    lifecycleGeneration += 1;
    return startSessionInternal(message, epoch);
  })().catch(error => {
    if (wasStartCancelled(epoch)) return cancelledStartResult();
    throw error;
  }).finally(() => {
    if (startingOperation === operation) startingOperation = null;
  });
  startingOperation = operation;
  return operation.promise;
}

async function sendToContent(tabId, message) {
  if (!Number.isInteger(tabId)) return;
  try {
    await chrome.tabs.sendMessage(tabId, { ...message, target: 'content' });
  } catch {
    // The content script may be unavailable while navigating.
  }
}

async function finishSession({ state = 'idle', message = 'Ready when you are', expectedSessionId = null } = {}) {
  const { session } = await getSession();
  if (expectedSessionId && session?.sessionId !== expectedSessionId) return;
  const tabId = session?.tabId;
  await writeSession(null);
  const status = await writeStatus({ state, message, mode: session?.preferences?.mode });
  await sendToContent(tabId, { type: 'CAPTURE_STOPPED' });
  return status;
}

async function stopSession(reason = 'user') {
  startEpoch += 1;
  lifecycleGeneration += 1;
  if (stoppingOperation) return stoppingOperation;
  const operation = (async () => {
    const { session } = await getSession();
    if (!session || session.phase === 'idle') {
      await finishSession({ state: 'idle', message: 'Ready when you are' });
      await closeOffscreenDocument();
      return { state: 'idle', message: 'Ready when you are' };
    }

    await writeSession({ ...session, phase: 'stopping' });
    await writeStatus({ state: 'starting', message: 'Stopping subtitles…', mode: session.preferences?.mode });
    if (await hasOffscreenDocument()) {
      await chrome.runtime.sendMessage({
        target: 'offscreen',
        type: 'STOP_CAPTURE',
        sessionId: session.sessionId,
        reason
      }).catch(() => {});
    }
    await closeOffscreenDocument();

    const message = reason === 'user' ? 'Subtitles stopped' :
      reason === 'tab-closed' ? 'Tab closed · capture stopped' :
      reason === 'navigation' ? 'Page changed · capture stopped' :
      reason === 'reload' ? 'Page reloaded · capture stopped' : 'Capture stopped';
    return finishSession({ state: 'idle', message, expectedSessionId: session.sessionId });
  })().finally(() => {
    if (stoppingOperation === operation) stoppingOperation = null;
  });
  stoppingOperation = operation;
  return operation;
}

async function recoverStartingSession(session, status, generation) {
  let offscreenExists = false;
  let offscreen = null;
  try {
    offscreenExists = await hasOffscreenDocument();
    if (offscreenExists) offscreen = await sendToOffscreen({ type: 'GET_STATE' });
  } catch {
    offscreen = null;
  }

  if (offscreen?.session?.sessionId === session.sessionId && offscreen.session.phase === 'active') {
    let tab = null;
    try { tab = await chrome.tabs.get(session.tabId); } catch { /* The captured tab may have closed. */ }
    const { session: latest, status: latestStatus } = await getSession();
    if (lifecycleGeneration !== generation || stoppingOperation || latest?.sessionId !== session.sessionId || latest.phase !== 'starting') return latestStatus;
    if (tab && isSupportedVideo(tab.url) && (!session.url || tab.url === session.url)) {
      await writeSession({ ...session, phase: 'active' });
      const { session: restored, status: restoredStatus } = await getSession();
      if (lifecycleGeneration !== generation || stoppingOperation || restored?.sessionId !== session.sessionId || restored.phase !== 'active') return restoredStatus;
      const recoveredStatus = ['listening', 'backpressure'].includes(offscreen.status?.state)
        ? offscreen.status
        : { state: 'listening', message: 'Listening · subtitles will appear on the video', mode: session.preferences?.mode };
      const activeStatus = await writeStatus(recoveredStatus);
      const { session: activeSession, status: currentStatus } = await getSession();
      if (lifecycleGeneration !== generation || stoppingOperation || activeSession?.sessionId !== session.sessionId || activeSession.phase !== 'active') return currentStatus;
      await sendToContent(session.tabId, { type: 'CAPTURE_STARTED', preferences: session.preferences });
      return activeStatus;
    }
  }

  const { session: latestBeforeStop, status: latestStatus } = await getSession();
  if (lifecycleGeneration !== generation || stoppingOperation || latestBeforeStop?.sessionId !== session.sessionId || latestBeforeStop.phase !== 'starting') return latestStatus;
  if (offscreenExists) {
    await sendToOffscreen({ type: 'STOP_CAPTURE', sessionId: session.sessionId, reason: 'recovery' }).catch(() => {});
  }
  const { session: latest, status: currentStatus } = await getSession();
  if (lifecycleGeneration !== generation || stoppingOperation || latest?.sessionId !== session.sessionId || latest.phase !== 'starting') return currentStatus;
  return finishSession({
    state: 'error',
    message: 'The previous subtitle start did not complete. You can start again.',
    expectedSessionId: session.sessionId
  });
}

async function reconcileStatus({ recoverStaleStart = false } = {}) {
  const { session, status } = await getSession();
  if (!session) return status;
  if (session.phase === 'starting') {
    if (startingOperation && !recoverStaleStart) return status;
    if (recoveringStartingOperation) return recoveringStartingOperation;
    const generation = lifecycleGeneration;
    const operation = recoverStartingSession(session, status, generation).finally(() => {
      if (recoveringStartingOperation === operation) recoveringStartingOperation = null;
    });
    recoveringStartingOperation = operation;
    return operation;
  }
  if (session.phase !== 'active') return status;
  if (!await hasOffscreenDocument()) {
    return await finishSession({ state: 'error', message: 'Audio capture stopped unexpectedly. Start subtitles again.' });
  }
  try {
    const offscreen = await chrome.runtime.sendMessage({ target: 'offscreen', type: 'GET_STATE' });
    if (!offscreen?.session || offscreen.session.sessionId !== session.sessionId) {
      return await finishSession({ state: 'idle', message: 'Audio capture ended' });
    }
    if (offscreen.status) return await writeStatus(offscreen.status);
  } catch {
    return await finishSession({ state: 'error', message: 'Audio capture stopped unexpectedly. Start subtitles again.' });
  }
  return status;
}

async function handleOffscreenMessage(message) {
  const { session } = await getSession();
  if (!session || message.sessionId !== session.sessionId) return { ok: false, ignored: true };

  if (message.type === 'OFFSCREEN_STATUS') {
    const state = ['connecting', 'listening', 'backpressure'].includes(message.state) ? message.state : 'connecting';
    const status = await writeStatus({ state, message: cleanMessage(message.message, 'Connecting to local service…'), mode: session.preferences?.mode, profile: message.profile });
    return { ok: true, status };
  }

  if (message.type === 'OFFSCREEN_SUBTITLE') {
    const original = typeof message.original === 'string' ? message.original.slice(0, 1200) : '';
    const translated = typeof message.translated === 'string' ? message.translated.slice(0, 1200) : '';
    if (!translated.trim()) return { ok: true, ignored: true };
    await sendToContent(session.tabId, {
      type: 'SUBTITLE',
      subtitle: {
        original,
        translated,
        latencyMs: Number.isFinite(message.latency_ms) ? Math.max(0, Math.min(message.latency_ms, 600000)) : null,
        id: typeof message.id === 'string' ? message.id.slice(0, 80) : ''
      }
    });
    return { ok: true };
  }

  if (message.type === 'OFFSCREEN_ERROR') {
    const errorMessage = cleanMessage(message.message, 'Local subtitle service error.');
    if (message.fatal) {
      if (await hasOffscreenDocument()) {
        await chrome.runtime.sendMessage({ target: 'offscreen', type: 'STOP_CAPTURE', sessionId: session.sessionId, reason: 'fatal' }).catch(() => {});
      }
      await closeOffscreenDocument();
      await finishSession({ state: 'error', message: errorMessage, expectedSessionId: session.sessionId });
    } else {
      await writeStatus({ state: 'connecting', message: errorMessage, mode: session.preferences?.mode });
      chrome.runtime.sendMessage({ target: 'popup', type: 'ERROR', message: errorMessage }).catch(() => {});
    }
    return { ok: true };
  }

  if (message.type === 'OFFSCREEN_ENDED') {
    const fatal = message.fatal === true;
    await closeOffscreenDocument();
    await finishSession({
      state: fatal ? 'error' : 'idle',
      message: cleanMessage(message.message, fatal ? 'Audio capture ended unexpectedly.' : 'Capture stopped'),
      expectedSessionId: session.sessionId
    });
    return { ok: true };
  }

  return { ok: false };
}

async function handleMessage(message, sender) {
  if (!message || typeof message !== 'object') return { ok: false, error: 'Invalid request.' };
  if (sender.id !== chrome.runtime.id) return { ok: false, error: 'Untrusted extension message.' };

  const popupUrl = chrome.runtime.getURL('popup.html');
  if (['GET_STATUS', 'START_REQUEST', 'STOP_REQUEST', 'SET_OVERLAY_PREFERENCES'].includes(message.type) && sender.url !== popupUrl) {
    return { ok: false, error: 'This action is available from the extension popup.' };
  }
  if (typeof message.type === 'string' && message.type.startsWith('OFFSCREEN_') && sender.url !== OFFSCREEN_URL) {
    return { ok: false, error: 'Untrusted capture message.' };
  }
  if (['NAVIGATED', 'CONTENT_READY'].includes(message.type) && !isTrustedContentSender(sender)) {
    return { ok: false, error: 'Untrusted page message.' };
  }

  if (message.type === 'CONTENT_READY') {
    const { session } = await getSession();
    if (session?.phase === 'active' && session.tabId === sender.tab.id) {
      await chrome.tabs.sendMessage(session.tabId, {
        target: 'content', type: 'CAPTURE_STARTED', preferences: session.preferences
      }, { frameId: sender.frameId }).catch(() => {});
    }
    return { ok: true };
  }

  if (message.type === 'GET_STATUS') {
    return { ok: true, status: await reconcileStatus() };
  }
  if (message.type === 'START_REQUEST') {
    try {
      return await startSession(message);
    } catch (error) {
      const errorMessage = cleanMessage(error?.message, 'Could not start subtitles.');
      await chrome.runtime.sendMessage({ target: 'popup', type: 'ERROR', message: errorMessage }).catch(() => {});
      return { ok: false, error: errorMessage };
    }
  }
  if (message.type === 'STOP_REQUEST') {
    return { ok: true, status: await stopSession('user') };
  }
  if (message.type === 'SET_OVERLAY_PREFERENCES') {
    const requestedPreferences = safePreferences(message.preferences);
    const { session } = await getSession();
    if (session && session.phase === 'active') {
      const preferences = {
        ...session.preferences,
        showJapanese: requestedPreferences.showJapanese,
        fontSize: requestedPreferences.fontSize
      };
      await writeSession({ ...session, preferences });
      await sendToContent(session.tabId, { type: 'SET_PREFERENCES', preferences });
    }
    return { ok: true };
  }
  if (message.type === 'NAVIGATED') {
    const { session } = await getSession();
    if (session && sender.tab?.id === session.tabId && (sender.frameId !== 0 || message.url !== session.url)) {
      await stopSession('navigation');
    }
    return { ok: true };
  }
  if (typeof message.type === 'string' && message.type.startsWith('OFFSCREEN_')) {
    return handleOffscreenMessage(message);
  }
  return { ok: false, error: 'Unknown extension message.' };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== 'background') return false;
  handleMessage(message, sender).then(sendResponse).catch(error => sendResponse({ ok: false, error: cleanMessage(error?.message) }));
  return true;
});

chrome.tabs.onRemoved.addListener(tabId => {
  getSession().then(({ session }) => {
    if (session?.tabId === tabId) return stopSession('tab-closed');
  }).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== 'loading' && typeof changeInfo.url !== 'string') return;
  getSession().then(({ session }) => {
    if (!session || session.tabId !== tabId) return;
    const reason = changeInfo.status === 'loading' ? 'reload' : 'navigation';
    return stopSession(reason);
  }).catch(() => {});
});

chrome.tabCapture.onStatusChanged.addListener(info => {
  if (!['stopped', 'error'].includes(info.status)) return;
  getSession().then(({ session }) => {
    if (session?.tabId === info.tabId && session.phase === 'active') {
      return stopSession(info.status === 'error' ? 'capture-error' : 'capture-ended');
    }
  }).catch(() => {});
});

chrome.runtime.onStartup.addListener(async () => {
  const { session } = await getSession();
  if (session) await finishSession({ state: 'idle', message: 'Browser restarted · capture stopped' });
});
