const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { existsSync } = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

// Exercises real extension/frame messaging and fullscreen with controlled pages.
// Capture state and captions are supplied directly; no inference service is needed.
async function main() {
  const root = path.resolve(__dirname, '..');
  const temporaryRoot = path.resolve(os.tmpdir());
  const temporary = await fs.mkdtemp(path.join(temporaryRoot, 'subtitle-vimeo-test-'));
  assert.equal(path.dirname(path.resolve(temporary)), temporaryRoot);
  const extension = path.join(temporary, 'extension');
  await fs.mkdir(extension);
  for (const name of await fs.readdir(path.join(root, 'extension'))) {
    if (name === 'manifest.json' || /\.(js|css|html)$/.test(name)) {
      await fs.copyFile(path.join(root, 'extension', name), path.join(extension, name));
    }
  }
  const installedChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const executablePath = process.env.CHROME_PATH ||
    (process.platform === 'win32' && existsSync(installedChrome) ? installedChrome : null);
  let context;
  try {
    context = await chromium.launchPersistentContext(path.join(temporary, 'profile'), {
      ...(executablePath ? { executablePath } : { channel: 'chromium' }),
      headless: true, viewport: { width: 1200, height: 800 },
      ignoreDefaultArgs: ['--disable-extensions'],
      args: [...(executablePath ? [] : [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]),
        '--enable-unsafe-extension-debugging']
    });
    context.setDefaultTimeout(10000);
    const errors = [];
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
    await context.route('http://127.0.0.1:8765/health', route => route.fulfill({
      json: { ok: true, supported_modes: ['context'] }
    }));
    await context.route('https://vimeo.com/**', route => route.fulfill({
      contentType: 'text/html', body: '<!doctype html><title>Vimeo video fixture</title><body style="margin:0"></body>'
    }));
    await context.route('https://player.vimeo.com/**', route => route.fulfill({
      contentType: 'text/html', body: `<!doctype html><title>Vimeo player fixture</title>
        <style>body{margin:0}#player{width:800px;height:450px;background:#234}.vp-video-wrapper{width:100%;height:100%;position:relative}video{width:100%;height:100%}button{position:absolute;top:10px;left:10px}</style>
        <div id="player"><div class="vp-video-wrapper"><video></video><button>Fullscreen</button></div></div>
        <script>document.querySelector('button').onclick=()=>document.querySelector('.vp-video-wrapper').requestFullscreen();</script>`
    }));
    const browserCDP = await context.browser().newBrowserCDPSession();
    let extensionId;
    if (executablePath) ({ id: extensionId } = await browserCDP.send('Extensions.loadUnpacked', { path: extension }));
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    extensionId ||= new URL(worker.url()).host;
    const page = context.pages()[0] || await context.newPage();
    await page.goto('https://vimeo.com/76979871');
    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    await popup.waitForFunction(() => localServiceOnline);
    const urls = ['https://vimeo.com/76979871', 'https://player.vimeo.com/video/76979871', 'https://vimeo.com/channels/staffpicks/76979871'];
    for (const url of urls) {
      assert.equal(await popup.evaluate(url => isSupportedVideo(url), url), true, url);
    }
    assert.equal(await popup.evaluate(() => isSupportedVideo('https://vimeo.com/')), false);
    await page.bringToFront();
    await popup.evaluate(() => getActiveTab());
    assert.equal(await popup.locator('#start-button').isEnabled(), true);
    console.log('PASS: the popup accepts Vimeo videos and enables Start.');
    await popup.close();
    const tabId = await worker.evaluate(async () => {
      const tabs = await chrome.tabs.query({ url: 'https://vimeo.com/*' });
      const id = tabs[0].id;
      await writeSession({
        sessionId: 'vimeo-fixture', tabId: id, url: tabs[0].url, phase: 'active',
        preferences: { showJapanese: true, fontSize: 'medium', mode: 'context' }
      });
      await sendToContent(id, { type: 'CAPTURE_STARTED', preferences: { showJapanese: true, fontSize: 'medium' } });
      return id;
    });
    await page.evaluate(() => {
      const iframe = document.createElement('iframe');
      iframe.src = 'https://player.vimeo.com/video/76979871';
      iframe.allowFullscreen = true;
      iframe.style.cssText = 'width:900px;height:600px;border:0';
      document.body.append(iframe);
    });
    const player = page.frameLocator('iframe');
    await player.locator('#local-stream-subtitles-overlay').waitFor();
    assert.equal(await page.locator('#local-stream-subtitles-overlay').count(), 0, 'Overlay belongs inside the player frame');
    await worker.evaluate(async () => {
      await handleOffscreenMessage({ type: 'OFFSCREEN_SUBTITLE', sessionId: 'vimeo-fixture', original: 'こんにちは', translated: 'Vimeo fixture caption' });
    });
    const cdp = await context.newCDPSession(page);
    await cdp.send('DOM.enable');
    await assertEventually(async () => JSON.stringify(await cdp.send('DOM.getDocument', { depth: -1, pierce: true })).includes('Vimeo fixture caption'));
    console.log('PASS: a late Vimeo iframe synchronizes and renders broadcast subtitles.');

    await player.getByRole('button', { name: 'Fullscreen' }).click();
    await assertEventually(() => player.locator('#local-stream-subtitles-overlay').evaluate(host => document.fullscreenElement?.contains(host)));
    await player.locator('#player').evaluate(() => document.exitFullscreen());
    await assertEventually(() => player.locator('#local-stream-subtitles-overlay').evaluate(host => host.parentElement.id === 'player'));
    console.log('PASS: Vimeo captions follow fullscreen and return to the player.');
    assert.equal(await worker.evaluate(async () => (await getSession()).session?.phase), 'active');

    await player.locator('#player').evaluate(() => history.pushState({}, '', '/video/12345678'));
    await player.locator('#local-stream-subtitles-overlay').waitFor({ state: 'detached' });
    await assertEventually(() => worker.evaluate(async () => !(await getSession()).session));
    assert.equal(await worker.evaluate(async () => (await getSession()).status.state), 'idle');
    console.log('PASS: Vimeo player navigation stops the session without browser errors.');

    await page.goto('https://player.vimeo.com/video/76979871');
    await worker.evaluate(async tabId => {
      await writeSession({ sessionId: 'direct-fixture', tabId, url: 'https://player.vimeo.com/video/76979871', phase: 'active' });
      await sendToContent(tabId, { type: 'CAPTURE_STARTED', preferences: {} });
    }, tabId);
    await page.locator('#local-stream-subtitles-overlay').waitFor();
    await worker.evaluate(async () => stopSession());
    await page.locator('#local-stream-subtitles-overlay').waitFor({ state: 'detached' });
    assert.deepEqual(errors, [], 'No browser runtime errors');
    console.log('PASS: direct Vimeo player tabs attach and remove captions on stop.');
  } finally {
    if (context) await context.close();
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

async function assertEventually(predicate) {
  const deadline = Date.now() + 10000;
  do {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  assert.fail('Timed out waiting for browser state');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
