# serv00 (FreeBSD) deployment package

This is a Python source archive, not a Docker image. serv00 does not run Linux Docker containers. The archive contains only application source, a minimal configuration template, and a foreground launcher. It does not include local cookies, credentials, or configuration.

Upload the archive to your serv00 home directory, then run:

```sh
tar -xzf gemini-web2api-serv00.tar.gz
cd gemini-web2api
cp config.example.json config.json
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
sh start.sh
```

`start.sh` runs the service in the background, while `sh run.sh` runs it in the foreground. `keepalive.sh` uses `flock` to avoid concurrent restarts and can be called from cron every five minutes. Its local health check restarts the service if either the API or the cookie-rotation worker has stopped.
Set a free port in `config.json` before starting; if exposing a port through serv00, reserve/configure it in the control panel. The template binds to `127.0.0.1` only. Configure authentication and any proxy/tunnel separately before exposing the API. Set `cookie_file` to a path on the server only after uploading credentials securely, and keep those files private. For unattended sessions, set `cookie_rotation_interval_sec` to `300`; the service calls Google's RotateCookies endpoint and atomically updates the JSON cookie file with mode 0600. Rotation requires a fresh authenticated export to start and cannot revive a rejected session. Stop the foreground process with Ctrl-C.

The modular service also supports model change monitoring. Set `model_check_interval_sec` to `21600` and `model_snapshot_file` to an absolute writable path such as `/home/igsshan/gemini-web2api/gemini-models.json`. The default is `0` (disabled), with a minimum enabled interval of one hour. After a 60-second startup delay, each due check makes only one model-list RPC request, never a generation request. Both successful and failed attempt times persist across restarts. A private atomic snapshot records the current models and latest additions, removals, renames, and metadata changes; authentication failures leave the valid baseline intact. Events are logged even when request logging is disabled. This task does not modify `/v1/models` or generation routing. See the main README for the full configuration and state fields.

The account has a 512 MB RAM quota and existing services use about 300 MB. This service may fit when idle but concurrent or large image requests can exceed the remaining capacity. This archive does not configure a background service or change remote processes.
