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

The default known-image SHA256 and dimensions refer to the successful HAR3 replay: JPEG 2816 x 1536. Adjust image validation before applying to another image.

## Method

1. Establish a complete control chain, checking the exact JPEG SHA256 and dimensions.
2. Start each case from the same recorded RPC URL, query and body. Remove one header field throughout the chain; all other headers stay at the full baseline. Keep the same HTTP Session and response Cookie jar.
3. Verify the field is truly absent on the wire. `curl_cffi`'s `None` header value explicitly suppresses default header restoration; empty strings would send empty values. Cookie tests also temporarily clear the Cookie jar, since jar-generated Cookie is not suppressed by the header exclusion alone. Restore the previous jar, including new response cookies, afterward.
4. Follow media hops immediately. Waiting 45 seconds between hops failed to preserve the successful control; following the same existing image's chain immediately succeeds. This observation does not establish the token's precise lifetime.
5. Wait at least **three minutes after every complete case** before starting the next. There is also an initial three-minute cooldown. A case has at most four sequential HTTP requests, no concurrency and no automatic retry. The expected average is at most about 1.3 requests per minute; each endpoint is called at most once per case.
6. Run a full successful control before the first group and after every group of at most five removals. The ending control also serves as the next group's starting control. Incomplete groups stay `awaiting_control`.
7. After the field screen, isolate failed fields at each stage where they normally occur, with the same whole-image verification and controls. A failure at the final hop after a field was removed upstream does not by itself prove which stage needed that field.
8. Stop on 429, 503, Retry-After, challenge markers, transport errors, failed positive controls, absent wire capture or failed header suppression. An operator can create a `STOP` file inside the results directory to stop before the next request.

A shared Cookie snapshot is loaded from the current service account into memory. Explicit login Cookie is sent only to `gemini.google.com` for the RPC and the exact mediator allowlist `lh3.google.com` / `work.fife.usercontent.google.com`. It is never explicitly forwarded to `lh3.googleusercontent.com`. The normal response Cookie jar is preserved. If credentials expire, the failed positive control stops the run.

The experiment covers HAR-exported headers plus the added Cookie. HTTP/2 pseudoheaders/authority and derived Content-Length are framing and are not removed. Passing individual removals does not establish that all passing fields can be removed simultaneously. Conclusions are limited to the tested account, image, session and transport.

## Results and duration

- `progress.json`: current status, request/case counts and next case time.
- `requests.jsonl`: one record per request, including case ID, wire header names, omission verification, hashes and response Cookie **names** (no values).
- `results.csv`, `report.md`: continuously updated whole-image outcomes and control classification.
- `manifest.json`: reproducible input hashes and experiment configuration.
- `NNNN-body`: bounded nonimage responses, which can contain signed URLs. These remain private. Repeated JPEG bodies are hashed rather than retained.

Directories use 0700 and files 0600. Do not publish raw response bodies or private HAR input files. Cookie values are never written to experiment results.

For approximately 34 fields, the initial screen takes about 2 hours 10 minutes at three minutes per case, including controls. Subsequent stage isolation adds three minutes per case and control. Expected total is about two to three hours if few fields fail, but validity is governed by successful controls rather than elapsed time. The runner does not resume or retry automatically.
