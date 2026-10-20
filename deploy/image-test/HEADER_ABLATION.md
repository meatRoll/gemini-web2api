# Existing-image header ablation

`header_ablation.py` runs separately from the service and never submits an image-generation request. It replays one stored `c8o8Fe` download RPC and an existing image's media chain. This diagnostic intentionally uses private replay inputs, which must not be committed.

Run with the same installed `curl_cffi` and service package used by the successful replay:

```sh
python header_ablation.py \
  --rpc-input /private/gemini-har-replay-input.json \
  --media-headers /private/har-media-headers.json \
  --config /private/service-config.json \
  --package-root /path/containing/gemini_web2api \
  --output /private/new-ablation-directory \
  --interval 45
```

Input formats:

- RPC input: array of `{ "har": "gemini.google.com3.har", "request": { "url": "...", "headers": { "name": "value" }, "body": "exact HAR postData.text" } }`.
- Media headers: `{ "media": { "name": "value" }, "mediator": { "name": "value" } }`, copied from HAR3 media fetches (entries 317 and 318).

The default known-image SHA256 and dimensions refer to the successful HAR3 replay, JPEG 2816 x 1536. For a different image, adjust the expected image validation before using the script.

The test freezes one service Cookie in memory for the experiment. It adds it only to `gemini.google.com` for the RPC and to the exact mediator hosts `lh3.google.com` / `work.fife.usercontent.google.com`. It never writes the Cookie to results or sends it to `lh3.googleusercontent.com`. The current service may continue normal Cookie rotation independently. If the frozen login or URLs expire, a failed positive control stops the run.

Method:

1. Establish a full chain and verify the final image's complete SHA256 and JPEG dimensions.
2. Test the authenticated mediator first, then RPC, source fetch, and final fetch.
3. Remove one recorded header at a time from the full baseline. Also test removal of the added Cookie at authenticated stages.
4. Suppress automatic header restoration using curl's explicit exclusion syntax, and verify actual outgoing header names through a callback. Values are discarded by the callback.
5. Keep at least 45 seconds **after completion** of each HTTP request before sending the next. There is no concurrency or automatic retry. CLI rejects intervals below 30 seconds.
6. Run complete positive controls before and after every group of up to five ablations. Rows in an interrupted group remain `awaiting_control`.
7. Stop on 429, 503, Retry-After, challenge markers, failed positive control, missing header capture, or unsuccessful header removal.

Only fields exported in the HAR plus the added Cookie are ablated. HTTP/2 pseudoheaders/authority and derived Content-Length are transport framing rather than optional HAR headers and are not removed.

A successful RPC ablation means it still returns an allowed source URL; a source/mediator ablation means an allowed relay URL is returned. These are per-stage results, not independent end-to-end checks for each RPC/relay case. Final-fetch tests require a byte-identical known image. The results do not establish that individually removable headers can all be removed simultaneously. Failed ablations between healthy controls are observations in this account/session, not universal protocol requirements.

Output:

- `progress.json`: current status, counts and next request time.
- `requests.jsonl`: stage, removed field, HTTP/semantic result, hashes and outgoing header **names**.
- `results.csv`, `report.md`: continuously updated per-field findings.
- `manifest.json`: experiment parameters and input hashes.
- `baseline-urls.json`, `NNNN-body`: private signed URLs / bounded response bodies. Directories use 0700 and files 0600; do not publish these.

At 45 seconds, HAR3's 120 ablations, 50 group controls and 5 chain controls take about 2 hours 15 minutes. To stop cleanly, create a `STOP` file inside the output directory. The program finishes the current request, stops before the next, and preserves partial results. It does not resume or retry automatically.
