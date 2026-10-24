(() => {
  const MESSAGE_TARGET = 'content';
  const OVERLAY_ID = 'local-stream-subtitles-overlay';
  const SUBTITLE_LIFETIME_MS = 8000;
  const ALLOWED_FONT_SIZES = { small: 22, medium: 28, large: 36 };

  let sessionActive = false;
  let currentSessionId = null;
  let overlayHost = null;
  let shadowRoot = null;
  let positionedPlayer = null;
  let previousInlinePosition = '';
  let currentCaption = null;
  const acknowledgedSegments = new Set();
  let currentPreferences = { showJapanese: true, fontSize: 'medium' };
  let clearTimer = null;
  let lastUrl = location.href;

  function safePreferences(input = {}) {
    return {
      showJapanese: input.showJapanese !== false,
      fontSize: Object.hasOwn(ALLOWED_FONT_SIZES, input.fontSize) ? input.fontSize : 'medium'
    };
  }

  function findPlayer() {
    const hostname = new URL(location.href).hostname;
    if (['youtube.com', 'www.youtube.com'].includes(hostname)) {
      return document.querySelector('#movie_player') || document.querySelector('ytd-player');
    }
    const video = document.querySelector('video');
    const player = document.querySelector('#player:not(iframe)') || video?.closest('.vp-video-wrapper') || video?.parentElement;
    const fullscreen = document.fullscreenElement;
    if (player && fullscreen && fullscreen.tagName !== 'VIDEO' && (player.contains(fullscreen) || fullscreen.contains(player))) {
      return fullscreen;
    }
    return player;
  }

  function ensureOverlay() {
    if (!sessionActive) return null;
    const player = findPlayer();
    if (!player) return null;

    if (!overlayHost) {
      overlayHost = document.createElement('div');
      overlayHost.id = OVERLAY_ID;
      overlayHost.setAttribute('aria-live', 'polite');
      overlayHost.setAttribute('role', 'status');
      overlayHost.style.cssText = [
        'position:absolute',
        'inset:0',
        'z-index:2147483647',
        'display:flex',
        'align-items:flex-end',
        'justify-content:center',
        'box-sizing:border-box',
        'padding:0 5% 10%',
        'pointer-events:none'
      ].join(';');
      shadowRoot = overlayHost.attachShadow({ mode: 'closed' });
      const style = document.createElement('style');
      style.textContent = `
        :host { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .stack { display: flex; width: min(100%, 1100px); flex-direction: column; align-items: center; gap: 5px; text-align: center; opacity: 0; transform: translateY(8px); transition: opacity 180ms ease, transform 180ms ease; }
        .stack.visible { opacity: 1; transform: translateY(0); }
        .line { max-width: 100%; padding: 5px 12px; border: 1px solid rgba(255,255,255,.08); border-radius: 8px; background: rgba(10, 12, 10, .84); color: #fff; font-size: var(--subtitle-size, 28px); font-weight: 650; line-height: 1.28; letter-spacing: .005em; text-shadow: 0 1px 3px rgba(0,0,0,.8); overflow-wrap: anywhere; box-decoration-break: clone; -webkit-box-decoration-break: clone; }
        .original { color: #e6e8e4; font-size: calc(var(--subtitle-size, 28px) * .68); font-weight: 500; }
        @media (max-width: 640px) { .line { font-size: min(var(--subtitle-size, 28px), 5vw); } .original { font-size: min(calc(var(--subtitle-size, 28px) * .68), 3.5vw); } }
        @media (prefers-reduced-motion: reduce) { .stack { transition: none; } }
      `;
      const stack = document.createElement('div');
      stack.className = 'stack';
      stack.part = 'subtitles';
      const original = document.createElement('div');
      original.className = 'line original';
      original.hidden = true;
      const translated = document.createElement('div');
      translated.className = 'line translated';
      stack.append(original, translated);
      shadowRoot.append(style, stack);
    }

    if (overlayHost.parentElement !== player) {
      restorePlayerPosition();
      const computed = getComputedStyle(player);
      if (computed.position === 'static') {
        positionedPlayer = player;
        previousInlinePosition = player.style.position;
        player.style.position = 'relative';
      }
      player.appendChild(overlayHost);
    }
    applyPreferences();
    if (currentCaption) renderCaption(currentCaption);
    return { host: overlayHost, stack: shadowRoot.querySelector('.stack') };
  }

  function applyPreferences() {
    if (!shadowRoot) return;
    const size = ALLOWED_FONT_SIZES[currentPreferences.fontSize] || ALLOWED_FONT_SIZES.medium;
    shadowRoot.querySelector('.stack').style.setProperty('--subtitle-size', `${size}px`);
    shadowRoot.querySelector('.original').hidden = !currentPreferences.showJapanese;
  }

  function showCaption(caption) {
    currentCaption = caption;
    const overlay = ensureOverlay();
    if (!overlay) return;
    renderCaption(caption);
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = setTimeout(() => clearCaption(), SUBTITLE_LIFETIME_MS);
  }

  function renderCaption(caption) {
    if (!shadowRoot) return;
    const stack = shadowRoot.querySelector('.stack');
    const original = shadowRoot.querySelector('.original');
    const translated = shadowRoot.querySelector('.translated');

    original.textContent = typeof caption.original === 'string' ? caption.original : '';
    translated.textContent = typeof caption.translated === 'string' ? caption.translated : '';
    original.hidden = !currentPreferences.showJapanese || !original.textContent.trim();
    stack.classList.remove('visible');
    requestAnimationFrame(() => {
      if (currentCaption !== caption || !sessionActive || !shadowRoot) return;
      shadowRoot.querySelector('.stack')?.classList.add('visible');
      scheduleReadableAcknowledgement(caption);
    });
  }

  function scheduleReadableAcknowledgement(caption) {
    const segmentId = caption.telemetry?.segment_id;
    if (!Number.isSafeInteger(segmentId) || segmentId < 1 || !currentSessionId ||
        currentCaption !== caption || acknowledgedSegments.has(`${currentSessionId}:${segmentId}`) || caption.readabilityCheckScheduled) return;
    caption.readabilityCheckScheduled = true;
    requestAnimationFrame(() => {
      caption.readabilityCheckScheduled = false;
      if (currentCaption !== caption || !sessionActive || !shadowRoot) return;
      if (document.visibilityState === 'hidden') return;
      const stack = shadowRoot.querySelector('.stack');
      const translated = shadowRoot.querySelector('.translated');
      if (!overlayHost?.isConnected || !stack?.classList.contains('visible') || !translated?.textContent.trim()) return;
      const opacity = Number(getComputedStyle(stack).opacity);
      if (!Number.isFinite(opacity) || opacity < 0.99) {
        caption.readabilityAttempts = (caption.readabilityAttempts || 0) + 1;
        if (caption.readabilityAttempts < 180) scheduleReadableAcknowledgement(caption);
        return;
      }
      const acknowledgementKey = `${currentSessionId}:${segmentId}`;
      acknowledgedSegments.add(acknowledgementKey);
      while (acknowledgedSegments.size > 128) {
        acknowledgedSegments.delete(acknowledgedSegments.values().next().value);
      }
      const readableAt = performance.timeOrigin + performance.now();
      chrome.runtime.sendMessage({
        target: 'background',
        type: 'SUBTITLE_RENDERED',
        sessionId: currentSessionId,
        segment_id: segmentId,
        first_readable_browser_ms: readableAt,
        final_browser_ms: readableAt
      }).catch(() => {});
    });
  }

  function clearCaption() {
    currentCaption = null;
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = null;
    if (shadowRoot) shadowRoot.querySelector('.stack').classList.remove('visible');
    setTimeout(() => {
      if (shadowRoot && !currentCaption) {
        shadowRoot.querySelector('.original').textContent = '';
        shadowRoot.querySelector('.translated').textContent = '';
      }
    }, 200);
  }

  function removeOverlay() {
    if (clearTimer) clearTimeout(clearTimer);
    clearTimer = null;
    currentCaption = null;
    overlayHost?.remove();
    overlayHost = null;
    shadowRoot = null;
    restorePlayerPosition();
  }

  function restorePlayerPosition() {
    if (positionedPlayer?.style.position === 'relative') {
      positionedPlayer.style.position = previousInlinePosition;
    }
    positionedPlayer = null;
    previousInlinePosition = '';
  }

  function stopOverlay() {
    sessionActive = false;
    currentSessionId = null;
    removeOverlay();
  }

  function handleMessage(message) {
    if (!message || message.target !== MESSAGE_TARGET) return;
    if (message.type === 'CAPTURE_STARTED') {
      sessionActive = true;
      currentSessionId = typeof message.sessionId === 'string' ? message.sessionId : null;
      currentPreferences = safePreferences(message.preferences);
      ensureOverlay();
    } else if (message.type === 'SUBTITLE' && message.subtitle && typeof message.subtitle === 'object') {
      if (!sessionActive || !currentSessionId || message.sessionId !== currentSessionId) return;
      const telemetry = message.subtitle.telemetry;
      const caption = {
        original: typeof message.subtitle.original === 'string' ? message.subtitle.original.slice(0, 1200) : '',
        translated: typeof message.subtitle.translated === 'string' ? message.subtitle.translated.slice(0, 1200) : '',
        telemetry: telemetry && Number.isSafeInteger(telemetry.segment_id) ? { segment_id: telemetry.segment_id } : null,
        readabilityCheckScheduled: false,
        readabilityAttempts: 0
      };
      if (caption.translated.trim()) showCaption(caption);
    } else if (message.type === 'SET_PREFERENCES') {
      currentPreferences = safePreferences(message.preferences);
      applyPreferences();
    } else if (message.type === 'CAPTURE_STOPPED') {
      stopOverlay();
    }
  }

  function checkNavigation() {
    const currentUrl = location.href;
    if (currentUrl === lastUrl) return;
    lastUrl = currentUrl;
    if (!sessionActive) return;
    stopOverlay();
    chrome.runtime.sendMessage({ target: 'background', type: 'NAVIGATED', url: currentUrl }).catch(() => {});
  }

  chrome.runtime.onMessage.addListener(handleMessage);
  document.addEventListener('fullscreenchange', () => ensureOverlay());
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'hidden' && currentCaption) scheduleReadableAcknowledgement(currentCaption);
  });
  window.addEventListener('popstate', checkNavigation, { passive: true });
  window.addEventListener('hashchange', checkNavigation, { passive: true });
  window.addEventListener('pagehide', () => {
    if (sessionActive) chrome.runtime.sendMessage({ target: 'background', type: 'NAVIGATED', url: '' }).catch(() => {});
    stopOverlay();
  }, { once: true });

  setInterval(checkNavigation, 750);
  setInterval(() => {
    if (sessionActive && currentCaption && !overlayHost?.isConnected) ensureOverlay();
  }, 1500);
  chrome.runtime.sendMessage({ target: 'background', type: 'CONTENT_READY' }).catch(() => {});
})();
