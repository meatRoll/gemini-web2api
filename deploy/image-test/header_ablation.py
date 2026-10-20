#!/usr/bin/env python3
"""Paced, one-header-at-a-time replay of an existing Gemini image (no generation).

Uses HAR export files and current service cookie in memory. Never sends login
cookies to googleusercontent.com. Run separately from the API service.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import re
import struct
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.new')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def jpeg_size(data):
    if not data.startswith(b'\xff\xd8'):
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            return None
        marker = data[offset + 1]
        offset += 2
        if marker in (0x01, 0xd8) or 0xd0 <= marker <= 0xd7:
            continue
        if marker in (0xd9, 0xda):
            return None
        length = int.from_bytes(data[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(data):
            return None
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                      0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
            return [width, height]
        offset += length
    return None


def captured_header_names(data):
    """Keep outgoing field names only; discard URL and all header values."""
    result = set()
    for line in data.decode('iso-8859-1').splitlines():
        match = re.match(r'^([A-Za-z0-9-]+):', line)
        if match:
            result.add(match.group(1).lower())
    return result


def initial_media_url(value):
    parts = urlsplit(value)
    path = parts.path
    if '=' not in path.rsplit('/', 1)[-1]:
        path += '=d-I'
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['alr'] = 'yes'
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


class StopExperiment(Exception):
    pass


class Experiment:
    def __init__(self, args):
        from curl_cffi import requests, CurlOpt
        sys.path.insert(0, args.package_root)
        from gemini_web2api.config import load_config
        load_config(args.config)
        from gemini_web2api.gemini import load_cookie, _batch_response_url
        self.parse_rpc = _batch_response_url
        self.requests, self.CurlOpt = requests, CurlOpt
        self.args = args
        self.root = Path(args.output)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.records = []
        self.rows = []
        self.last_request = 0.0
        self.seq = 0
        self.cookie, _ = load_cookie()
        if not self.cookie:
            raise StopExperiment('missing_current_cookie')
        rpc_entries = json.loads(Path(args.rpc_input).read_text())
        self.rpc = next(item['request'] for item in rpc_entries if item['har'] == args.har)
        self.profiles = json.loads(Path(args.media_headers).read_text())
        self.stages = {}
        self.baseline_sha = args.image_sha256
        self.started = time.time()
        self.status = 'starting'
        self.rng = random.Random(20261020)
        write_json(self.root / 'manifest.json', {
            'har': args.har, 'interval_seconds': args.interval,
            'cookie_sha256': digest(self.cookie.encode())[:16],
            'rpc_body_sha256': digest(self.rpc['body'].encode()),
            'expected_image_sha256': self.baseline_sha,
            'new_generation': False, 'egress': 'panel2_direct',
            'scope': 'single-header necessity in the tested account/session only',
            'wire_absence_checked': True,
            'rpc_success_definition': 'c8o8Fe returns a valid allowed source URL; not a per-case full image download',
            'media_success_definition': 'allowed relay URL; final hop must match exact known JPEG SHA256 and dimensions',
            'protected_control_hosts': ['gemini.google.com', 'lh3.google.com', 'work.fife.usercontent.google.com'],
        })
        self.publish()

    def publish(self):
        write_json(self.root / 'progress.json', {
            'status': self.status, 'requests': self.seq,
            'completed_ablations': len(self.rows),
            'total_ablations': sum(len(stage['headers']) for stage in self.stages.values()) or None,
            'elapsed_seconds': round(time.time() - self.started),
            'last_record': self.records[-1] if self.records else None,
            'next_request_not_before_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + max(0, self.args.interval - (time.monotonic() - self.last_request)))) if self.last_request else None,
        })
        with (self.root / 'results.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['stage', 'removed_header', 'wire_absent', 'status', 'semantic_success', 'classification', 'request_seq'])
            writer.writeheader()
            writer.writerows(self.rows)
        lines = ['# Paced HAR3 header ablation', '',
                 f'Status: {self.status}. No image generation. At least {self.args.interval:g} seconds between completed HTTP requests.', '',
                 'Each row removes one field from the complete baseline, not from the previous row. Passing alone does not establish that multiple passing fields can all be removed together.', '',
                 'c8 and relay rows test their immediate semantic response. Only final-image rows check the complete known JPEG SHA256. Successful controls bracket each group; interrupted groups remain inconclusive.', '',
                 '| Stage | Removed header | Verified absent on wire | HTTP | Semantic success | Classification |',
                 '| --- | --- | --- | --- | --- | --- |']
        for row in self.rows:
            lines.append('| {stage} | {removed_header} | {wire_absent} | {status} | {semantic_success} | {classification} |'.format(**row))
        (self.root / 'report.md').write_text('\n'.join(lines) + '\n')

    def pace(self):
        while True:
            if (self.root / 'STOP').exists():
                raise StopExperiment('operator_stop_file')
            remaining = self.args.interval - (time.monotonic() - self.last_request)
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1))

    def valid_url(self, value):
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in
                {'gemini.google.com', 'lh3.googleusercontent.com', 'lh3.google.com', 'work.fife.usercontent.google.com'}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            raise StopExperiment('unexpected_response_url')
        return parsed

    def request(self, stage_name, omitted=None, control=False):
        stage = self.stages[stage_name]
        self.valid_url(stage['url'])
        headers = dict(stage['headers'])
        if omitted:
            # curl_cffi None emits "field:" to suppress the field entirely.
            # An empty string would instead emit an empty-valued header.
            headers[omitted] = None
        wire_names = set()
        def debug(kind, data):
            if kind == 2:
                wire_names.update(captured_header_names(data))
        self.pace()
        self.seq += 1
        record = {'seq': self.seq, 'stage': stage_name, 'removed_header': omitted,
                  'control': control, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'request_host': urlsplit(stage['url']).hostname,
                  'request_url_sha256': digest(stage['url'].encode())[:16]}
        raw = b''
        next_url = None
        try:
            with self.requests.Session(impersonate='chrome', default_headers=False,
                    curl_options={self.CurlOpt.VERBOSE: 1, self.CurlOpt.DEBUGFUNCTION: debug}) as session:
                response = session.request(stage['method'], stage['url'], headers=headers,
                    data=stage.get('body'), timeout=25, allow_redirects=False,
                    accept_encoding='', max_recv_speed=2 * 1024 * 1024)
                if len(response.content) > 10 * 1024 * 1024:
                    raise StopExperiment('response_size_limit')
                raw = response.content
                mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
                record.update(status=response.status_code, mime=mime, bytes=len(raw),
                    body_sha256=digest(raw), wire_header_names=sorted(wire_names),
                    wire_absent=omitted.lower() not in wire_names if omitted else None,
                    retry_after_present='retry-after' in response.headers)
                if stage_name == 'rpc' and response.status_code == 200:
                    try:
                        next_url = self.parse_rpc(response.text)
                        self.valid_url(next_url)
                    except ValueError:
                        pass
                elif stage_name != 'final' and response.status_code == 200 and mime == 'text/plain':
                    text = raw.decode('utf-8', errors='replace').strip()
                    if len(text) <= 8192 and text.startswith('https://'):
                        self.valid_url(text)
                        next_url = text
                if next_url:
                    record['returned_url_host'] = urlsplit(next_url).hostname
                    record['returned_url_path_len'] = len(urlsplit(next_url).path)
                if stage_name == 'final':
                    record['dimensions'] = jpeg_size(raw)
                    record['semantic_success'] = (response.status_code == 200 and mime == 'image/jpeg'
                        and digest(raw) == self.baseline_sha and record['dimensions'] == [2816, 1536])
                else:
                    record['semantic_success'] = next_url is not None
                # Do not retain JPEGs repeatedly; keep bounded diagnostic response bodies.
                if not mime.startswith('image/'):
                    (self.root / f'{self.seq:04d}-body').write_bytes(raw[:16384])
                response.close()
        except StopExperiment:
            raise
        except Exception as exc:
            record.update(exception_type=type(exc).__name__, status=None, semantic_success=False,
                wire_absent=omitted.lower() not in wire_names if omitted and wire_names else None)
        finally:
            # Pace from completion, so even slow calls cannot produce bursts.
            self.last_request = time.monotonic()
        self.records.append(record)
        with (self.root / 'requests.jsonl').open('a') as stream:
            stream.write(json.dumps(record) + '\n')
        self.publish()
        print(json.dumps(record), flush=True)
        lower = raw.lower()
        if (record.get('status') in (429, 503) or record.get('retry_after_present')
                or any(token in lower for token in (b'unusual traffic', b'recaptcha', b'cf-chl-', b'captcha challenge'))):
            raise StopExperiment('rate_limit_or_challenge')
        if not wire_names:
            raise StopExperiment('outgoing_header_capture_missing')
        if omitted and not record.get('wire_absent'):
            raise StopExperiment('removed_header_was_resent')
        return record, next_url

    def control(self, stage):
        record, url = self.request(stage, control=True)
        if not record['semantic_success']:
            raise StopExperiment('positive_control_failed_' + stage)
        return url

    def establish_chain(self):
        rpc_headers = {k.lower(): v for k, v in self.rpc['headers'].items()}
        rpc_headers['cookie'] = self.cookie
        self.stages['rpc'] = {'url': self.rpc['url'], 'method': 'POST', 'headers': rpc_headers,
                              'body': self.rpc['body'].encode('utf-8')}
        source = self.control('rpc')
        self.stages['source'] = {'url': initial_media_url(source), 'method': 'GET',
                                  'headers': {k.lower(): v for k, v in self.profiles['media'].items()}}
        mediator = self.control('source')
        # Only this exact Google-domain allowlist may receive login cookies.
        if urlsplit(mediator).hostname not in {'lh3.google.com', 'work.fife.usercontent.google.com'}:
            raise StopExperiment('unexpected_authenticated_mediator_host')
        mediator_headers = {k.lower(): v for k, v in self.profiles['mediator'].items()}
        mediator_headers['cookie'] = self.cookie
        self.stages['mediator'] = {'url': mediator, 'method': 'GET', 'headers': mediator_headers}
        final = self.control('mediator')
        self.stages['final'] = {'url': final, 'method': 'GET',
                                 'headers': {k.lower(): v for k, v in self.profiles['media'].items()}}
        self.control('final')
        # Signed URLs remain private, account Cookie is not written to disk.
        write_json(self.root / 'baseline-urls.json', {k: v['url'] for k, v in self.stages.items()})
        self.status = 'running'
        self.publish()

    def run_stage(self, name):
        self.status = 'running_' + name
        names = sorted(self.stages[name]['headers'])
        self.rng.shuffle(names)
        if 'cookie' in names:
            names.remove('cookie')
            names.insert(0, 'cookie')
        for offset in range(0, len(names), self.args.group_size):
            group = names[offset:offset + self.args.group_size]
            self.control(name)
            added = []
            for omitted in group:
                record, _ = self.request(name, omitted)
                row = {'stage': name, 'removed_header': omitted,
                       'wire_absent': record.get('wire_absent'), 'status': record.get('status'),
                       'semantic_success': record['semantic_success'], 'classification': 'awaiting_control',
                       'request_seq': record['seq']}
                self.rows.append(row); added.append(row); self.publish()
            self.control(name)
            for row in added:
                row['classification'] = ('removal_passed_this_sample' if row['semantic_success']
                                         else 'removal_failed_between_healthy_controls')
            self.publish()

    def run(self):
        try:
            self.establish_chain()
            # Highest-value authenticated intermediary first.
            for name in ['mediator', 'rpc', 'source', 'final']:
                self.run_stage(name)
            self.control('final')
            self.status = 'completed'
        except StopExperiment as exc:
            self.status = 'stopped_' + str(exc)
        except Exception as exc:
            self.status = 'stopped_exception_' + type(exc).__name__
        finally:
            self.publish()
            print(json.dumps({'status': self.status, 'requests': self.seq,
                'completed_ablations': len(self.rows), 'output': str(self.root)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rpc-input', required=True)
    parser.add_argument('--media-headers', required=True)
    parser.add_argument('--har', default='gemini.google.com3.har')
    parser.add_argument('--config', required=True)
    parser.add_argument('--package-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--interval', type=float, default=45)
    parser.add_argument('--group-size', type=int, default=5)
    parser.add_argument('--image-sha256', default='673a7253133214e00edd8421cb5261ddb17dd7f2f3ea65cc74a5c6c9a3adba39')
    args = parser.parse_args()
    if args.interval < 30:
        parser.error('interval must be at least 30 seconds')
    if not 1 <= args.group_size <= 5:
        parser.error('group size must be 1..5')
    os.umask(0o077)
    # Hold one local lock across runs to prevent accidental concurrent probes.
    import fcntl
    lock_dir = Path(args.output).parent.parent
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_dir / 'header-ablation.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another header ablation is already running')
        Experiment(args).run()


if __name__ == '__main__':
    main()
