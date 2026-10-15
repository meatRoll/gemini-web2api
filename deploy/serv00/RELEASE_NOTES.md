# Serv00 native release 1.1.1

This release packages the modular Python server for FreeBSD 14.3 amd64 with Python 3.11. It includes all 13 dependency wheels and installs without Docker, root access, compilation, or package downloads. `release.json` records the source commit and stable release version; `SHA256SUMS.json` covers every packaged file except itself. The installer verifies those files before installing dependencies.

## Image input and agent compatibility

- Uses the current Gemini attachment structure, authenticated page session/XSRF tokens, and Chrome transport for image generation requests.
- Bundles `curl_cffi 0.16.3` and `libcurl-impersonate 2.2.2` with BoringSSL, built and loaded on Serv00 itself.
- Supports OpenAI image inputs and images returned by client tools, including pi agent's read-image workflow.
- Preserves dynamic model selection, agent request budgets, one-attempt agent calls, and SSE heartbeats. Image SSE delivers the completed answer as one content chunk.
- Stops on authentication/rate-limit responses and structured Gemini rejections instead of retrying them.
- Downloads public HTTPS images with validated DNS, redirect/size limits, and MIME checks.

Real Serv00 validation passed for ordinary and SSE image requests with `gemini-3.8-flash`, and for a tool-result image with `gemini-4-argon` through the existing public API. The user also confirmed the pi workflow. These probes are not run by the installer or launcher.

## Production deployment

Use `config.example.json` for a new production deployment. Reserve a TCP port in Serv00 even for loopback, configure authentication, and point `cookie_file` at a private authenticated export. The production template enables daily model checks and five-minute cookie rotation, and limits generation attempts to one. The separate `config.image-test.example.json` disables background upstream requests for controlled validation.

For an existing installation, keep the current `config.json`, authentication export, model snapshot, cache, API keys and tunnel configuration. Save the previous source and virtual environment before replacing them, and stop the API process under the same `.keepalive.lock` used by cron before switching code. Install dependencies into a fresh virtual environment before the switch. Verify the local health endpoint reports version 1.1.1 and that the existing public model-list route is available. No generation request is necessary for these checks.

This release targets `python -m gemini_web2api`. The legacy single-file entry point and Cloudflare Worker do not include the new attachment transport.
