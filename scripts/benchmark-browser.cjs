// Real tab capture against an intercepted local fixture in a disposable Chrome profile.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { existsSync } = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { chromium } = require('playwright');

async function main() {
  const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, item, index, all) => {
    if (index % 2 === 0) pairs.push([item.replace(/^--/, ''), all[index + 1]]);
    return pairs;
  }, []));
  const root = path.resolve(__dirname, '..');
  const url = args.url || 'ws://127.0.0.1:8765/listen';
  if (url !== 'ws://127.0.0.1:8765/listen') throw new Error('Chrome capture requires the extension default ws://127.0.0.1:8765/listen.');
  const health = await fetch('http://127.0.0.1:8765/health').then(r => r.json());
  assert.equal(health.ok, true, 'Start the warmed local service before browser replay.');
  assert.equal(health.active, false, 'An existing capture is active; browser replay will not interrupt it.');
  const manifestPath = path.resolve(args.manifest);
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  const samples = [];
  for (const sample of manifest.samples) {
    const audio = await fs.readFile(path.resolve(path.dirname(manifestPath), sample.path));
    assert.equal(createHash('sha256').update(audio).digest('hex'), sample.sha256, `Frozen audio changed: ${sample.id}`);
    samples.push({ ...sample, audio });
  }
  const repeats = Number(args.repeats || 1);
  const timeout = Number(args.timeout || 60) * 1000;
  assert.ok(Number.isInteger(repeats) && repeats > 0 && timeout > 0);
  const runtimeRoot = path.resolve(root, '.runtime');
  await fs.mkdir(runtimeRoot, { recursive: true });
  const profile = await fs.mkdtemp(path.join(runtimeRoot, 'latency-browser-'));
  const installedChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
  const executable = process.env.CHROME_PATH || (process.platform === 'win32' && existsSync(installedChrome) ? installedChrome : null);
  let context;
  const records = [];
  const output = path.resolve(args.output);
  try {
    context = await chromium.launchPersistentContext(profile, {
      ...(executable ? { executablePath: executable } : { channel: 'chromium' }),
      headless: true, viewport: { width: 1200, height: 800 },
      ignoreDefaultArgs: ['--disable-extensions', '--mute-audio'],
      args: [...(executable ? [] : [`--disable-extensions-except=${path.join(root, 'extension')}`, `--load-extension=${path.join(root, 'extension')}`]),
        '--enable-unsafe-extension-debugging', '--autoplay-policy=no-user-gesture-required']
    });
    const browserCDP = await context.browser().newBrowserCDPSession();
    let extensionId;
    if (executable) ({ id: extensionId } = await browserCDP.send('Extensions.loadUnpacked', { path: path.join(root, 'extension') }));
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    extensionId ||= new URL(worker.url()).host;
    const diagnostics = type => worker.evaluate(type => chrome.runtime.sendMessage({ target: 'offscreen', type }), type);
    let currentSample;
    await context.route('https://www.youtube.com/**', async route => {
      if (new URL(route.request().url()).pathname === '/sample.wav') {
        await route.fulfill({ contentType: 'audio/wav', body: currentSample.audio });
      } else {
        await route.fulfill({ contentType: 'text/html', body: `<!doctype html><meta charset="utf-8"><title>Frozen local latency replay</title>
          <style>body{margin:0;background:#151515}#movie_player{position:relative;height:700px}</style>
          <div id="movie_player"><button id="play">Play recorded sample</button><audio id="audio" preload="auto" src="/sample.wav"></audio></div>
          <script>document.querySelector('#play').onclick=()=>document.querySelector('#audio').play();</script>` });
      }
    });
    const video = context.pages()[0] || await context.newPage();
    for (let iteration = 1; iteration <= repeats; iteration += 1) {
      for (const sample of samples) {
        currentSample = sample;
        await video.goto(`https://www.youtube.com/watch?v=latency-${sample.id}-${iteration}`);
        await video.bringToFront();
        const { targetInfos } = await browserCDP.send('Target.getTargets', { filter: [{ type: 'tab', exclude: false }] });
        const target = targetInfos.find(info => info.url === video.url());
        assert.ok(target, 'Controlled replay tab exists.');
        await browserCDP.send('Extensions.triggerAction', { id: extensionId, targetId: target.targetId });
        // Toolbar action grants capture. Use its identical popup document for reliable automation.
        const popup = await context.newPage();
        await popup.goto(`chrome-extension://${extensionId}/popup.html`);
        await popup.getByText('Connected on this PC', { exact: true }).waitFor({ timeout });
        await video.bringToFront();
        await popup.evaluate(() => getActiveTab());
        await popup.evaluate(() => document.querySelector('#start-button').click());
        await popup.waitForFunction(() => document.querySelector('#session-dot').classList.contains('is-active'), null, { timeout });
        let metrics;
        const calibrationDeadline = Date.now() + 5000;
        do {
          metrics = await diagnostics('GET_METRICS');
          if (metrics?.clockUncertaintyMs != null) break;
          await new Promise(resolve => setTimeout(resolve, 100));
        } while (Date.now() < calibrationDeadline);
        assert.equal(metrics?.observability, true, 'Enable latency_observability in the local service settings.');
        assert.ok(metrics.clockUncertaintyMs != null, 'Browser/server clock calibration completed.');
        await video.getByRole('button', { name: 'Play recorded sample' }).click();
        await video.waitForFunction(() => document.querySelector('#audio').ended, null, { timeout: timeout + sample.audio_seconds * 1000 });
        // Worklet and resampling packetization must finish delivering the silent tail.
        await new Promise(resolve => setTimeout(resolve, 350));
        assert.equal((await diagnostics('END_BENCHMARK')).ok, true);
        const deadline = Date.now() + timeout;
        let renderDeadline = null;
        do {
          metrics = await diagnostics('GET_METRICS');
          const rendered = new Set(metrics.events.filter(event => event.outcome === 'rendered').map(event => event.segment_id));
          const emitted = metrics.counters.emitted || 0;
          if (metrics.benchmarkComplete && rendered.size === emitted) break;
          if (metrics.benchmarkComplete) renderDeadline ??= Date.now() + 2000;
          if (renderDeadline && Date.now() >= renderDeadline) break;
          await new Promise(resolve => setTimeout(resolve, 100));
        } while (Date.now() < deadline);
        assert.equal(metrics.benchmarkComplete, true, 'Live pipeline drained within timeout.');
        assert.equal(metrics.lostEvents || 0, 0, 'Diagnostics did not lose timing events.');
        const record = { sample_id: sample.id, sha256: sample.sha256, iteration,
          profile: health.profile, device: health.device, events: metrics.events, counters: metrics.counters,
          clock_uncertainty_ms: metrics.clockUncertaintyMs,
          rendered_segments: new Set(metrics.events.filter(e => e.outcome === 'rendered').map(e => e.segment_id)).size };
        record.unrendered_segments = Math.max(0, (record.counters.emitted || 0) - record.rendered_segments);
        record.measurement_complete = record.unrendered_segments === 0;
        records.push(record);
        await fs.mkdir(path.dirname(output), { recursive: true });
        await fs.writeFile(output, JSON.stringify({ schema_version: 1, records }, null, 2) + '\n');
        await popup.evaluate(() => document.querySelector('#stop-button').click());
        await popup.waitForFunction(() => document.querySelector('#stop-button').disabled, null, { timeout });
        await popup.close();
        console.log(`Recorded ${sample.id}, round ${iteration}: ${record.counters.emitted || 0} captions, ${record.rendered_segments} visible.`);
      }
    }
  } finally {
    if (context) await context.close();
    const resolved = path.resolve(profile);
    if (resolved.startsWith(runtimeRoot + path.sep) && path.basename(resolved).startsWith('latency-browser-')) {
      await fs.rm(resolved, { recursive: true, force: true });
    }
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
