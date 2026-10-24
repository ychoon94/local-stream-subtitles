const assert = require('node:assert/strict');
const { readFile } = require('node:fs/promises');
const { existsSync } = require('node:fs');
const { chromium } = require('playwright');

async function main() {
  const contentScript = await readFile(require('node:path').resolve(__dirname, '../extension/content.js'), 'utf8');
  const installedChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const executable = process.env.CHROME_PATH ||
    (process.platform === 'win32' && existsSync(installedChrome) ? installedChrome : null);
  const browser = await chromium.launch({
    ...(executable ? { executablePath: executable } : { channel: 'chromium' }), headless: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
    await page.route('https://www.youtube.com/**', route => route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html><head><style>
        body { margin: 0; background: #171a18; }
        #fixture { width: 900px; height: 540px; margin: 60px auto; }
        #movie_player { position: relative; width: 900px; height: 500px; background: #282e2a; }
      </style></head><body><div id="fixture"><div id="movie_player"></div></div></body></html>`
    }));
    await page.addInitScript(`
      window.__runtimeMessages = [];
      const listeners = [];
      Object.defineProperty(window, 'chrome', { configurable: true, value: { runtime: {
        onMessage: { addListener(listener) { listeners.push(listener); } },
        sendMessage(message) { window.__runtimeMessages.push(message); return Promise.resolve(); }
      } } });
      window.__deliverContentMessage = message => {
        window.__lastDeliveredSegmentId = message.subtitle?.telemetry?.segment_id || null;
        listeners[0]?.(message);
      };
    ` + contentScript);
    await page.goto('https://www.youtube.com/watch?v=render-visibility-test');
    await page.bringToFront();
    await page.waitForFunction(() => typeof window.__deliverContentMessage === 'function');
    assert.equal(await page.evaluate(() => document.visibilityState), 'visible', 'Fixture page is foregrounded.');
    await page.evaluate(() => window.__deliverContentMessage({
      target: 'content', type: 'CAPTURE_STARTED', sessionId: 'browser-session',
      preferences: { showJapanese: true, fontSize: 'medium' }
    }));

    const cases = [
      { label: 'display:none player', target: 'movie_player', property: 'display', hidden: 'none', visible: '' },
      { label: 'visibility:hidden ancestor', target: 'fixture', property: 'visibility', hidden: 'hidden', visible: '' },
      { label: 'zero-opacity ancestor', target: 'fixture', property: 'opacity', hidden: '0', visible: '' },
      { label: 'offscreen caption box', target: 'fixture', property: 'transform', hidden: 'translateX(-2000px)', visible: '' }
    ];

    for (const [index, testCase] of cases.entries()) {
      const segmentId = index + 1;
      await page.evaluate(({ segmentId, target, property, hidden }) => {
        document.getElementById(target).style[property] = hidden;
        window.__deliverContentMessage({
          target: 'content', type: 'SUBTITLE', sessionId: 'browser-session',
          subtitle: { original: '日本語', translated: `Visible after recovery ${segmentId}`, telemetry: { segment_id: segmentId } }
        });
      }, { segmentId, ...testCase });
      await page.waitForFunction(id => window.__lastDeliveredSegmentId === id, segmentId);
      await page.waitForTimeout(600);
      assert.equal(await page.evaluate(id => window.__runtimeMessages.some(message =>
        message.type === 'SUBTITLE_RENDERED' && message.segment_id === id), segmentId), false,
      `${testCase.label} must not emit a false render acknowledgement.`);

      await page.evaluate(({ target, property, visible }) => {
        document.getElementById(target).style[property] = visible;
      }, testCase);
      await page.waitForFunction(id => window.__runtimeMessages.some(message =>
        message.type === 'SUBTITLE_RENDERED' && message.segment_id === id), segmentId, { timeout: 4000 });
      console.log(`PASS: ${testCase.label} waits for a visible text box before acknowledging.`);
    }
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
