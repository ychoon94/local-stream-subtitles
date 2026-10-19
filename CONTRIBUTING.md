# Contributing

Small, reproducible fixes are welcome. This is an early Windows/NVIDIA preview;
other operating systems and hardware profiles need their own validation.

For a subtitle bug, include the extension version, operating system, GPU/VRAM,
speech and translation model names, source languages, and expected versus actual
behavior. For a public video, include a URL and timestamp if possible. A short
example of an incorrect caption is more useful than a full transcript.

Do not attach private audio, browser profiles, complete logs, or local settings.
Remove names or content you do not want published. Never include the pairing
token from `.runtime/settings.json` or `extension/local-config.json`.

For a pull request:

1. Keep the change focused and explain the behavior it fixes.
2. Add a regression test when the change affects capture, routing, or cleanup.
3. Run `npm ci`, `npm test`, and the backend tests described in the README.
4. On Windows, run `powershell -NoProfile -File tests/test_launchers.ps1`.
5. Describe checks you could not run. GPU/browser integration needs local hardware;
   unit tests must not download models or call a paid API.

The repository publishes source and installer scripts. Do not add model weights,
runtime binaries, generated pairing files, browser profiles, or recordings. By
submitting a contribution, you agree to license it under this project's MIT License.
Third-party software and model terms remain separate; see `THIRD_PARTY_NOTICES.md`.
