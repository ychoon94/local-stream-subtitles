# Security

This preview captures audio only after the user starts it on a YouTube tab.
The extension connects to the local subtitle service on `127.0.0.1:8765`.
That service requires an extension origin and a local pairing token; its Ollama
connection is fixed to `127.0.0.1:11434`. Do not expose either port to a network.

The application processes normal session audio and text in memory. Its setup
scripts download dependencies and models; third-party tools may write diagnostic
logs locally. Development benchmarks deliberately save results for public test
samples. Review logs before sharing them.

Never publish `.runtime/settings.json`, `extension/local-config.json`, `.env`
files, or a browser test profile. Setup generates fresh local pairing information
for each installation. If it is exposed, stop the services, delete the two local
pairing files, rerun setup, and reload the extension before restarting.

For a suspected vulnerability, use the repository's **Security → Report a
vulnerability** option if it is enabled. Otherwise open an issue asking for a
private reporting channel without posting exploit details or secrets. No response
time or supported-release commitment is offered for this early preview.
