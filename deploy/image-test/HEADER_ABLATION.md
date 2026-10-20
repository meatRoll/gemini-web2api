# Existing-image header ablation

`header_ablation.py` runs separately from the service and never submits an image-generation request. It replays HAR3's stored `c8o8Fe` download RPC and follows the returned media chain to the known image. Private inputs must not be committed.

```sh
python header_ablation.py \
  --rpc-input /private/gemini-har-replay-input.json \
  --media-headers /private/har-media-headers.json \
  --config /private/service-config.json \
  --package-root /path/containing/gemini_web2api \
  --output /private/new-ablation-directory \
  --interval 180
```

Input formats:

- RPC input: array of `{ "har": "gemini.google.com3.har", "request": { "url": "...", "headers": { "name": "value" }, "body": "exact HAR postData.text" } }`.
- Media headers: `{ "media": { "name": "value" }, "mediator": { "name": "value" } }`, copied from HAR3 entries 317 and 318.

The default known-image validation accepts the two already verified HAR3 JPEG variants: 2816 x 1536 with SHA256 `673a7253...` or `887cb010...`. Add `--image-sha256 HASH` to append another independently verified variant; use `--skip-field NAME` to resume after a confirmed field without retesting it. Skipped fields are recorded in the manifest and are not treated as newly tested.

## Method

1. Establish a complete control chain, checking the exact JPEG SHA256 and dimensions.
2. Start each case from the same recorded RPC URL, query and body. Remove one header field throughout the chain; all other HAR headers stay at the full baseline. Create a fresh HTTP Session and empty response Cookie jar for every case. Share that Session only within its four-hop chain, then close it and discard its response cookies.
3. After the cooldown, read the current service-account Cookie for that case rather than freezing an old value for the entire run. Record only its hash so credential rotation is visible. This is a controlled test of current authentication, not a byte-for-byte freeze of an expiring credential. Verify the omitted field is truly absent on the wire. `curl_cffi`'s `None` header value suppresses default header restoration; empty strings would send empty values. Cookie tests also temporarily empty the Cookie jar, since jar-generated Cookie is not suppressed by the header exclusion alone.
4. Follow media hops immediately. Waiting 45 seconds between hops failed to preserve the successful control; following the same existing image's chain immediately succeeds. This observation does not establish the token's precise lifetime.
5. Wait at least **three minutes after every complete case** before starting the next. There is also an initial three-minute cooldown. A case has at most four sequential HTTP requests, no concurrency and no automatic retry. The expected average is at most about 1.3 requests per minute; each endpoint is called at most once per case.
6. Run a full successful control before the first removal and, by default, immediately after each removal. The ending control also serves as the next experiment's preceding control. `--group-size` defaults to 1; larger groups up to five require an explicit override. If a control fails, unconfirmed rows are marked `inconclusive_control_not_completed` and the run stops.
7. After the field screen, isolate failed fields at each stage where they normally occur, with the same whole-image verification and controls. A failure at the final hop after a field was removed upstream does not by itself prove which stage needed that field.
8. Stop on 429, 503, Retry-After, challenge markers, transport errors, failed positive controls, absent wire capture or failed header suppression. An operator can create a `STOP` file inside the results directory to stop before the next request.

The current service Cookie is re-read before each case and remains fixed inside that case. Explicit login Cookie is sent only to `gemini.google.com` for the RPC and the exact mediator allowlist `lh3.google.com` / `work.fife.usercontent.google.com`. It is never explicitly forwarded to `lh3.googleusercontent.com`. Response cookies are preserved only within that case's Session. If current authentication or another full-control condition fails, the run stops. Cookie-hash changes alone do not establish credential expiry.

The experiment covers HAR-exported headers plus the added Cookie. HTTP/2 pseudoheaders/authority and derived Content-Length are framing and are not removed. Passing individual removals does not establish that all passing fields can be removed simultaneously. Conclusions are limited to the tested account, image, session and transport.

## Results and duration

- `progress.json`: current test, total and confirmed counts, a heartbeat every 15 seconds, and next case time.
- `requests.jsonl`: one record per request, including case ID, wire header names, omission verification, hashes and response Cookie **names** (no values).
- `results.csv`, `report.md`: continuously updated whole-image outcomes and control classification.
- `manifest.json`: reproducible input hashes and experiment configuration.
- `NNNN-body`: bounded nonimage responses, which can contain signed URLs. These remain private. Repeated JPEG bodies are hashed rather than retained.

Directories use 0700 and files 0600. Do not publish raw response bodies or private HAR input files. Cookie values are never written to experiment results.

For 34 fields and the default one-removal-per-control policy, the initial screen takes approximately 3.5 hours at three minutes per case (34 removals and 35 complete controls). Use `--skip-field` for already confirmed fields when resuming. Subsequent stage isolation adds three minutes per case and control. Failed removals may end their chain early but still receive the full cooldown. The runner does not resume or retry automatically.
