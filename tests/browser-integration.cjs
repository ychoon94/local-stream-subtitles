const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { existsSync } = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

// Uses real Chrome tab capture and real local inference against a controlled
// YouTube-origin fixture. No personal browser profile or live stream is opened.
async function main() {
  const root = path.resolve(__dirname, '..');
  const extension = path.join(root, 'extension');
  const results = path.join(root, 'test-results');
  await fs.mkdir(results, { recursive: true });
  const health = await fetch('http://127.0.0.1:8765/health').then(r => r.json());
  assert.equal(health.ok, true, 'Start the real local service first.');
  const audio = await fs.readFile(path.join(root, '.runtime/streaming-sample.wav'));
  const errors = [];
  const installedChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const browserExecutable = process.env.CHROME_PATH ||
    (process.platform === 'win32' && existsSync(installedChrome) ? installedChrome : null);
  const context = await chromium.launchPersistentContext(path.join(root, '.runtime/browser-test-profile'), {
    ...(browserExecutable ? { executablePath: browserExecutable } : { channel: 'chromium' }),
    headless: true, viewport: { width: 1200, height: 800 },
    ignoreDefaultArgs: ['--disable-extensions', '--mute-audio'],
    args: [...(browserExecutable ? [] : [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]),
      '--enable-unsafe-extension-debugging', '--autoplay-policy=no-user-gesture-required'],
  });
  const watchdog = setTimeout(() => { console.error('Browser integration exceeded 120 seconds.'); process.exit(1); }, 120000);
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  try {
    const browserCDP = await context.browser().newBrowserCDPSession();
    let loadedId;
    if (browserExecutable) ({ id: loadedId } = await browserCDP.send('Extensions.loadUnpacked', { path: extension }));
    console.log('Browser launched and unpacked extension loaded.');
    await context.route('https://www.youtube.com/**', async route => {
      if (route.request().url().endsWith('/local-test.wav')) {
        await route.fulfill({ contentType: 'audio/wav', body: audio });
      } else {
        await route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head><meta charset="utf-8"><title>Local Japanese audio test</title>
          <style>body{margin:0;background:#171a18;color:#e5e9e6;font-family:system-ui}main{margin:60px auto;max-width:960px}#movie_player{position:relative;height:540px;background:#282e2a;display:grid;place-items:center}h1{font-size:24px}button{padding:12px 20px}p{color:#b6c1b9}</style></head>
          <body><main><h1>Local Japanese audio test</h1><p>Public JSUT sample · real tab audio capture · local inference</p>
          <div id="movie_player"><button id="play">Play Japanese sample</button><audio id="audio" src="/local-test.wav" loop></audio></div><button id="fullscreen">Fullscreen</button></main>
          <script>document.querySelector('#play').onclick=()=>document.querySelector('#audio').play();document.querySelector('#fullscreen').onclick=()=>document.querySelector('#movie_player').requestFullscreen();</script></body></html>` });
      }
    });
    const workers = context.serviceWorkers();
    const worker = workers[0];
    const extensionId = loadedId || (worker ? new URL(worker.url()).host :
      new URL((await context.waitForEvent('serviceworker')).url()).host);
    console.log(`Extension registered (${extensionId}).`);
    const video = context.pages()[0] || await context.newPage();
    video.on('pageerror', error => errors.push(error.message));
    await video.goto('https://www.youtube.com/watch?v=local-subtitle-test');
    await video.getByRole('button', { name: 'Play Japanese sample' }).click();
    await video.bringToFront();
    const videoCDP = await context.newCDPSession(video);
    const { targetInfos } = await browserCDP.send('Target.getTargets', { filter: [{ type: 'tab', exclude: false }] });
    const tabTarget = targetInfos.find(info => info.url === video.url()) || targetInfos[0];
    assert.ok(tabTarget, 'YouTube test tab target exists.');
    const popupPromise = context.waitForEvent('page', { predicate: page => page.url().includes('popup.html'), timeout: 1500 }).catch(() => null);
    await browserCDP.send('Extensions.triggerAction', { id: extensionId, targetId: tabTarget.targetId });
    let popup = await popupPromise;
    if (!popup) {
      // Native toolbar popups may be CDP "other" targets, which Playwright
      // doesn't expose as pages. Test the same document without changing the
      // active captured tab; triggerAction above grants the real user action.
      popup = await context.newPage();
      await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    }
    console.log('Extension popup document opened after toolbar action.');
    await popup.waitForLoadState();
    await popup.getByText('Connected on this PC', { exact: true }).waitFor();
    await video.bringToFront();
    await popup.evaluate(() => getActiveTab());
    assert.equal(await popup.locator('#pairing-section').isVisible(), false, 'Automatic local pairing');
    assert.equal(await popup.locator('input[value="direct"]').isDisabled(), true);
    await popup.locator('body').screenshot({ path: path.join(results, 'extension-popup.png') });
    await popup.evaluate(() => document.querySelector('#start-button').click());
    await popup.waitForFunction(() => ['is-active', 'is-error'].some(name => document.querySelector('#session-dot').classList.contains(name)), null, { timeout: 20000 });
    assert.ok(await popup.locator('#session-dot').evaluate(dot => dot.classList.contains('is-active')),
      await popup.locator('#session-message').textContent());
    await video.waitForSelector('#local-stream-subtitles-overlay', { timeout: 10000 });

    const deadline = Date.now() + 25000;
    let captionFound = false;
    await videoCDP.send('DOM.enable');
    while (Date.now() < deadline) {
      const dom = await videoCDP.send('DOM.getDocument', { depth: -1, pierce: true });
      if (JSON.stringify(dom).includes('Malaysia')) { captionFound = true; break; }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    await video.screenshot({ path: path.join(results, 'subtitles-on-video.png') });
    if (!captionFound) {
      await fs.writeFile(path.join(results, 'video-dom.json'), JSON.stringify(await videoCDP.send('DOM.getDocument', { depth: -1, pierce: true })));
      console.log('Session status:', await popup.locator('#session-message').textContent());
    }
    assert.equal(captionFound, true, 'Real captured Japanese audio yields English subtitle.');
    assert.equal(await video.locator('audio').evaluate(audio => !audio.paused), true);
    console.log('PASS: extension loads, pairs, captures real tab audio, and displays local English subtitles.');

    await video.getByRole('button', { name: 'Fullscreen', exact: true }).click();
    assert.equal(await video.evaluate(() => document.fullscreenElement?.contains(document.querySelector('#local-stream-subtitles-overlay'))), true);
    await video.screenshot({ path: path.join(results, 'subtitles-fullscreen.png') });
    await video.evaluate(() => document.exitFullscreen());
    console.log('PASS: subtitle overlay stays inside the fullscreen player.');

    await popup.evaluate(() => document.querySelector('#stop-button').click());
    await video.waitForSelector('#local-stream-subtitles-overlay', { state: 'detached' });
    await popup.waitForFunction(() => document.querySelector('#stop-button').disabled);
    const stoppedHealth = await fetch('http://127.0.0.1:8765/health').then(r => r.json());
    assert.equal(stoppedHealth.active, false);
    assert.deepEqual(errors, [], 'No browser runtime errors.');
    console.log('PASS: stop removes subtitles, closes capture, and releases backend session.');

    await video.bringToFront();
    await popup.evaluate(() => document.querySelector('#start-button').click());
    await popup.waitForFunction(() => document.querySelector('#session-dot').classList.contains('is-active'));
    await video.waitForSelector('#local-stream-subtitles-overlay');
    await video.evaluate(() => history.pushState({}, '', '/watch?v=another-video'));
    await video.waitForSelector('#local-stream-subtitles-overlay', { state: 'detached' });
    await popup.waitForFunction(() => document.querySelector('#stop-button').disabled);
    const navigationHealth = await fetch('http://127.0.0.1:8765/health').then(r => r.json());
    assert.equal(navigationHealth.active, false);
    console.log('PASS: restart works and YouTube SPA navigation stops capture.');
  } finally {
    await context.close();
    clearTimeout(watchdog);
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
