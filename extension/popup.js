const HEALTH_URL = 'http://127.0.0.1:8765/health';
const LOCAL_CONFIG_URL = 'local-config.json';

const elements = {
  indicator: document.querySelector('#connection-indicator'),
  serviceStatus: document.querySelector('#service-status'),
  refreshService: document.querySelector('#refresh-service'),
  pageHint: document.querySelector('#page-hint'),
  pairingSection: document.querySelector('#pairing-section'),
  pairingToken: document.querySelector('#pairing-token'),
  toggleToken: document.querySelector('#toggle-token'),
  showJapanese: document.querySelector('#show-japanese'),
  fontSize: document.querySelector('#font-size'),
  start: document.querySelector('#start-button'),
  stop: document.querySelector('#stop-button'),
  sessionMessage: document.querySelector('#session-message'),
  sessionDot: document.querySelector('#session-dot')
};

let activeTab = null;
let localServiceOnline = false;
let packagedToken = '';
let sessionStatus = { state: 'idle', message: 'Ready when you are' };
let pendingStart = false;
let pendingStop = false;
let supportedModes = new Set(['context']);

function selectedMode() {
  return document.querySelector('input[name="mode"]:checked')?.value === 'direct' ? 'direct' : 'context';
}

function setSessionStatus(status) {
  if (!status || typeof status !== 'object') return;
  sessionStatus = {
    state: typeof status.state === 'string' ? status.state : 'idle',
    message: typeof status.message === 'string' ? status.message : 'Ready when you are'
  };
  elements.sessionMessage.textContent = sessionStatus.message;
  elements.sessionDot.className = 'session-dot';

  if (['listening', 'backpressure'].includes(sessionStatus.state)) {
    elements.sessionDot.classList.add('is-active');
  } else if (sessionStatus.state === 'error') {
    elements.sessionDot.classList.add('is-error');
  } else if (['checking', 'starting', 'connecting'].includes(sessionStatus.state)) {
    elements.sessionDot.classList.add('is-busy');
  }
  renderButtons();
}

function renderButtons() {
  const busy = pendingStart || pendingStop || ['checking', 'starting', 'connecting'].includes(sessionStatus.state);
  const running = !['idle', 'error'].includes(sessionStatus.state);
  elements.start.disabled = busy || running || !localServiceOnline || !supportedModes.has(selectedMode()) || !isSupportedVideo(activeTab?.url);
  elements.stop.disabled = pendingStop || !running;
  document.querySelectorAll('input[name="mode"]').forEach(input => {
    input.disabled = pendingStart || pendingStop || running || (input.value === 'direct' && !supportedModes.has('direct'));
  });
}

function applySupportedModes(modes) {
  if (Array.isArray(modes)) {
    supportedModes = new Set(modes.filter(mode => mode === 'context' || mode === 'direct'));
    supportedModes.add('context');
  }
  const direct = document.querySelector('input[name="mode"][value="direct"]');
  const directCard = direct.closest('.mode-card');
  const directDescription = directCard.querySelector('.mode-description');
  direct.disabled = !supportedModes.has('direct');
  directCard.title = direct.disabled ? 'The installed speech model supports context translation.' : '';
  directDescription.textContent = direct.disabled ? 'Unavailable for this model' : 'Closer to the Japanese';
  if (direct.disabled && direct.checked) {
    document.querySelector('input[name="mode"][value="context"]').checked = true;
    chrome.storage.local.set({ mode: 'context' }).catch(() => {});
  }
  renderButtons();
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

function setServiceState(online, message) {
  localServiceOnline = online;
  elements.serviceStatus.textContent = message;
  elements.indicator.className = `connection-indicator ${online ? 'is-online' : 'is-offline'}`;
  elements.indicator.setAttribute('aria-label', online ? 'Local service online' : 'Local service offline');
  renderButtons();
}

async function checkLocalService() {
  setServiceState(false, 'Checking on this PC…');
  elements.refreshService.disabled = true;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1800);
    const response = await fetch(HEALTH_URL, { cache: 'no-store', signal: controller.signal });
    clearTimeout(timeout);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const health = await response.json().catch(() => null);
    applySupportedModes(health?.supported_modes);
    setServiceState(true, 'Connected on this PC');
  } catch {
    applySupportedModes(['context']);
    setServiceState(false, 'Local service not found');
  } finally {
    elements.refreshService.disabled = false;
  }
}

async function loadToken() {
  try {
    const response = await fetch(chrome.runtime.getURL(LOCAL_CONFIG_URL), { cache: 'no-store' });
    if (response.ok) {
      const config = await response.json();
      if (config && typeof config.token === 'string' && config.token.trim()) packagedToken = config.token.trim();
    }
  } catch {
    packagedToken = '';
  }

  const stored = await chrome.storage.local.get(['pairingToken']);
  if (packagedToken) {
    elements.pairingSection.hidden = true;
    elements.pairingToken.value = '';
  } else {
    elements.pairingSection.hidden = false;
    elements.pairingToken.value = typeof stored.pairingToken === 'string' ? stored.pairingToken : '';
  }
}

async function loadPreferences() {
  const saved = await chrome.storage.local.get(['mode', 'showJapanese', 'fontSize']);
  const mode = saved.mode === 'direct' ? 'direct' : 'context';
  const radio = document.querySelector(`input[name="mode"][value="${mode}"]`);
  if (radio) radio.checked = true;
  elements.showJapanese.checked = saved.showJapanese !== false;
  elements.fontSize.value = ['small', 'medium', 'large'].includes(saved.fontSize) ? saved.fontSize : 'medium';
}

async function savePreferences() {
  const preference = {
    mode: selectedMode(),
    showJapanese: elements.showJapanese.checked,
    fontSize: elements.fontSize.value
  };
  await chrome.storage.local.set(preference);
  chrome.runtime.sendMessage({ target: 'background', type: 'SET_OVERLAY_PREFERENCES', preferences: preference }).catch(() => {});
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTab = tab || null;
  if (isSupportedVideo(activeTab?.url)) {
    elements.pageHint.textContent = 'Ready on this video.';
    elements.pageHint.classList.remove('is-error');
  } else {
    elements.pageHint.textContent = 'Open a YouTube video, live stream, or Vimeo video to use subtitles.';
    elements.pageHint.classList.remove('is-error');
  }
  renderButtons();
}

async function requestStart() {
  if (!activeTab?.id || !isSupportedVideo(activeTab.url)) return;
  if (!supportedModes.has(selectedMode())) {
    elements.pageHint.textContent = 'This translation style is unavailable for the installed speech model.';
    elements.pageHint.classList.add('is-error');
    return;
  }
  const token = packagedToken || elements.pairingToken.value.trim();
  if (!token) {
    elements.pageHint.textContent = 'Enter the local pairing token to continue.';
    elements.pageHint.classList.add('is-error');
    elements.pairingToken.focus();
    return;
  }

  pendingStart = true;
  elements.pageHint.textContent = '';
  elements.pageHint.classList.remove('is-error');
  setSessionStatus({ state: 'starting', message: 'Starting subtitles…' });
  renderButtons();

  try {
    if (!packagedToken) await chrome.storage.local.set({ pairingToken: token });
    const preferences = {
      mode: selectedMode(),
      showJapanese: elements.showJapanese.checked,
      fontSize: elements.fontSize.value
    };
    await chrome.storage.local.set(preferences);
    const result = await chrome.runtime.sendMessage({
      target: 'background',
      type: 'START_REQUEST',
      tabId: activeTab.id,
      url: activeTab.url,
      token,
      preferences
    });
    if (result?.cancelled) {
      if (result.status) setSessionStatus(result.status);
      return;
    }
    if (!result?.ok) throw new Error(result?.error || 'Could not start subtitles.');
    if (result.status) setSessionStatus(result.status);
  } catch (error) {
    setSessionStatus({ state: 'error', message: error instanceof Error ? error.message : 'Could not start subtitles.' });
  } finally {
    pendingStart = false;
    renderButtons();
  }
}

async function requestStop() {
  pendingStop = true;
  setSessionStatus({ state: 'starting', message: 'Stopping subtitles…' });
  renderButtons();
  try {
    const result = await chrome.runtime.sendMessage({ target: 'background', type: 'STOP_REQUEST', reason: 'user' });
    if (result?.status) setSessionStatus(result.status);
  } catch {
    setSessionStatus({ state: 'error', message: 'Could not stop subtitles. Close this tab to end capture.' });
  } finally {
    pendingStop = false;
    renderButtons();
  }
}

function showStatusError(message) {
  elements.pageHint.textContent = message;
  elements.pageHint.classList.add('is-error');
}

elements.refreshService.addEventListener('click', checkLocalService);
elements.start.addEventListener('click', requestStart);
elements.stop.addEventListener('click', requestStop);
elements.toggleToken.addEventListener('click', () => {
  const reveal = elements.pairingToken.type === 'password';
  elements.pairingToken.type = reveal ? 'text' : 'password';
  elements.toggleToken.textContent = reveal ? 'Hide' : 'Show';
  elements.toggleToken.setAttribute('aria-label', `${reveal ? 'Hide' : 'Show'} pairing token`);
});

document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', savePreferences));
elements.showJapanese.addEventListener('change', savePreferences);
elements.fontSize.addEventListener('change', savePreferences);
elements.pairingToken.addEventListener('input', () => {
  if (elements.pairingToken.value.trim()) elements.pageHint.classList.remove('is-error');
});

chrome.runtime.onMessage.addListener(message => {
  if (message?.target !== 'popup') return;
  if (message.type === 'STATUS_UPDATE' && message.status) setSessionStatus(message.status);
  if (message.type === 'ERROR' && typeof message.message === 'string') showStatusError(message.message);
});

async function initialize() {
  const [, , statusResponse] = await Promise.allSettled([loadPreferences(), loadToken(), chrome.runtime.sendMessage({ target: 'background', type: 'GET_STATUS' })]);
  if (statusResponse.status === 'fulfilled' && statusResponse.value?.status) setSessionStatus(statusResponse.value.status);
  await Promise.all([getActiveTab(), checkLocalService()]);
}

initialize().catch(error => {
  setSessionStatus({ state: 'error', message: 'Extension setup could not finish. Reload this popup.' });
  console.error('Popup initialization failed:', error);
});
