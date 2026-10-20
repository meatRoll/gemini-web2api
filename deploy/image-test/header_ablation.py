#!/usr/bin/env python3
"""Slow per-header replay of an existing Gemini image, without generation.

A case follows the whole short-lived media chain immediately, then waits at
least three minutes before the next case. Private HAR inputs are not included.
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
    """Retain names only. Never log the outgoing URL or header values."""
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
    stages = ('rpc', 'source', 'mediator', 'final')

    def __init__(self, args):
        from curl_cffi import requests, CurlOpt
        sys.path.insert(0, args.package_root)
        from gemini_web2api.config import load_config
        load_config(args.config)
        from gemini_web2api.gemini import load_cookie, _batch_response_url
        self.args = args
        self.parse_rpc = _batch_response_url
        self.load_current_cookie = load_cookie
        self.root = Path(args.output)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        (self.root / 'requests.jsonl').touch(mode=0o600)
        self.cookie, _ = load_cookie()
        if not self.cookie:
            raise StopExperiment('missing_current_cookie')
        entries = json.loads(Path(args.rpc_input).read_text())
        self.rpc = next(item['request'] for item in entries if item['har'] == args.har)
        self.profiles = json.loads(Path(args.media_headers).read_text())
        rpc_headers = {k.lower(): v for k, v in self.rpc['headers'].items()}
        rpc_headers['cookie'] = self.cookie
        mediator_headers = {k.lower(): v for k, v in self.profiles['mediator'].items()}
        mediator_headers['cookie'] = self.cookie
        media_headers = {k.lower(): v for k, v in self.profiles['media'].items()}
        self.headers = {'rpc': rpc_headers, 'source': media_headers,
                        'mediator': mediator_headers, 'final': media_headers}
        self.fields = sorted(set().union(*(set(h) for h in self.headers.values())))
        self.wire_names = set()
        def debug(kind, data):
            if kind == 2:
                self.wire_names.update(captured_header_names(data))
        self.make_session = lambda: requests.Session(
            impersonate='chrome', default_headers=False,
            curl_options={CurlOpt.VERBOSE: 1, CurlOpt.DEBUGFUNCTION: debug})
        self.session = None
        self.current_test = None
        self.rows = []
        self.records = []
        self.seq = self.case_id = 0
        self.started = time.time()
        # Initial wait also prevents a restart from following prior probes rapidly.
        self.last_case_end = time.monotonic()
        self.status = 'waiting_initial_cooldown'
        self.rng = random.Random(20261020)
        write_json(self.root / 'manifest.json', {
            'har': args.har, 'interval_between_cases_seconds': args.interval,
            'initial_cookie_sha256': digest(self.cookie.encode())[:16],
            'credential_policy': 'read current service Cookie after cooldown before each case; log its hash only',
            'rpc_body_sha256': digest(self.rpc['body'].encode()),
            'expected_image_sha256': args.image_sha256,
            'new_generation': False, 'egress': 'panel2_direct',
            'fields': self.fields, 'scope': 'necessity in this account/session only',
            'wire_absence_checked': True, 'case_success': 'exact known JPEG bytes and dimensions',
            'session_scope': 'fresh Session and empty response Cookie jar per case; shared only within its media chain',
            'media_cookies': 'explicit login Cookie only on exact Google mediator allowlist',
            'pacing': 'at most four sequential requests per case, no parallelism or retries; cooldown after each case',
        })
        self.publish()

    def publish(self):
        remaining = max(0, self.args.interval - (time.monotonic() - self.last_case_end))
        write_json(self.root / 'progress.json', {
            'status': self.status, 'requests': self.seq, 'cases': self.case_id,
            'completed_ablations': len(self.rows), 'total_chain_fields': len(self.fields),
            'confirmed_ablations': sum(row['classification'].startswith('removal_') for row in self.rows),
            'current_test': self.current_test,
            'elapsed_seconds': round(time.time() - self.started),
            'last_record': self.records[-1] if self.records else None,
            'next_case_not_before_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + remaining)),
        })
        with (self.root / 'results.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['scope', 'removed_header', 'case_id',
                'jpeg_verified', 'first_failed_stage', 'classification'])
            writer.writeheader()
            writer.writerows(self.rows)
        lines = ['# Existing-image header ablation', '',
            f'Status: {self.status}. No image generation. At least {self.args.interval:g} seconds after each case.', '',
            'Each case removes one field from the full baseline and requires the exact known JPEG. Chain cases omit it wherever it occurs; failed fields are subsequently isolated by stage. Other headers and the recorded RPC body/query stay unchanged.', '',
            'A fresh HTTP Session and current account Cookie are used per case. Session response cookies are retained within the chain and discarded afterward. Authentication hashes are recorded; values are not. Curl field restoration and jar-generated Cookie are explicitly suppressed for the selected field, and outgoing names are checked. Short-lived media hops are followed immediately; there are no parallel calls or retries.', '',
            'Successful controls bracket each group. Pending groups are inconclusive. Individual removability does not prove that all passing fields may be removed together.', '',
            '| Scope | Removed field | Exact JPEG | First failed stage | Classification |',
            '| --- | --- | --- | --- | --- |']
        for row in self.rows:
            lines.append('| {scope} | {removed_header} | {jpeg_verified} | {first_failed_stage} | {classification} |'.format(**row))
        (self.root / 'report.md').write_text('\n'.join(lines) + '\n')

    def wait(self):
        self.status = 'waiting_between_cases'
        self.publish()
        next_heartbeat = time.monotonic() + 15
        while True:
            if (self.root / 'STOP').exists():
                raise StopExperiment('operator_stop_file')
            remaining = self.args.interval - (time.monotonic() - self.last_case_end)
            if remaining <= 0:
                return
            if time.monotonic() >= next_heartbeat:
                self.publish()
                next_heartbeat = time.monotonic() + 15
            time.sleep(min(remaining, 1))

    def check_url(self, value, stage):
        p = urlsplit(value)
        allowed = {'rpc': {'gemini.google.com'},
                   'source': {'lh3.googleusercontent.com'},
                   'mediator': {'lh3.google.com', 'work.fife.usercontent.google.com'},
                   'final': {'lh3.googleusercontent.com'}}
        if (p.scheme != 'https' or p.hostname not in allowed[stage]
                or p.username is not None or p.password is not None or p.port not in (None, 443)):
            raise StopExperiment('unexpected_response_url_' + stage)

    def request(self, stage, url, omitted=None):
        self.check_url(url, stage)
        headers = dict(self.headers[stage])
        if omitted:
            # None suppresses the field; empty string would send an empty value.
            headers[omitted] = None
        self.wire_names = set()
        saved_cookies = None
        if omitted == 'cookie':
            # A jar-generated Cookie bypasses HTTPHEADER exclusion alone.
            saved_cookies = self.session.cookies
            self.session.cookies = {}
        self.seq += 1
        record = {'seq': self.seq, 'case_id': self.case_id, 'stage': stage,
                  'removed_header': omitted, 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                  'account_cookie_sha256': digest(self.cookie.encode())[:16],
                  'host': urlsplit(url).hostname, 'url_sha256': digest(url.encode())[:16]}
        try:
            response = self.session.request('POST' if stage == 'rpc' else 'GET', url,
                headers=headers, data=self.rpc['body'].encode() if stage == 'rpc' else None,
                timeout=25, allow_redirects=False, accept_encoding='', max_recv_speed=2*1024*1024)
            try:
                raw = response.content
                if len(raw) > 10*1024*1024:
                    raise StopExperiment('response_size_limit')
                mime = response.headers.get('content-type', '').split(';')[0].strip().lower()
                record.update(status=response.status_code, mime=mime, bytes=len(raw),
                    body_sha256=digest(raw), wire_header_names=sorted(self.wire_names),
                    wire_absent=omitted not in self.wire_names if omitted else None,
                    response_cookie_names=sorted({c.name for c in response.cookies.jar}))
                next_url = None
                if response.status_code == 200 and stage == 'rpc':
                    try:
                        next_url = self.parse_rpc(response.text)
                    except ValueError:
                        pass
                elif response.status_code == 200 and stage != 'final' and mime == 'text/plain':
                    text = raw.decode('utf-8', errors='replace').strip()
                    if len(text) <= 8192 and text.startswith('https://'):
                        next_url = text
                if stage == 'final':
                    record['dimensions'] = jpeg_size(raw)
                    success = (response.status_code == 200 and mime == 'image/jpeg'
                        and digest(raw) == self.args.image_sha256 and record['dimensions'] == [2816,1536])
                else:
                    success = next_url is not None
                record['stage_success'] = success
                if not mime.startswith('image/'):
                    (self.root / f'{self.seq:04d}-body').write_bytes(raw[:16384])
                if (response.status_code in (429,503) or 'retry-after' in response.headers
                        or (not mime.startswith('image/') and any(t in raw.lower() for t in
                            (b'unusual traffic', b'recaptcha', b'cf-chl-', b'captcha challenge')))):
                    raise StopExperiment('rate_limit_or_challenge')
                if not self.wire_names:
                    raise StopExperiment('outgoing_header_capture_missing')
                if omitted and not record['wire_absent']:
                    raise StopExperiment('removed_header_was_resent')
                return success, next_url
            finally:
                response.close()
        except StopExperiment:
            raise
        except Exception as exc:
            record['exception_type'] = type(exc).__name__
            raise StopExperiment('transport_error_' + type(exc).__name__) from None
        finally:
            if saved_cookies is not None:
                saved_cookies.update(self.session.cookies)
                self.session.cookies = saved_cookies
            self.records.append(record)
            with (self.root / 'requests.jsonl').open('a') as stream:
                stream.write(json.dumps(record) + '\n')
            self.publish()
            print(json.dumps(record), flush=True)

    def case(self, omitted=None, scope='chain'):
        self.current_test = {'scope': scope, 'removed_header': omitted,
                             'role': 'ablation' if omitted else 'control'}
        self.wait()
        self.case_id += 1
        self.status = 'running_case'
        self.cookie, _ = self.load_current_cookie()
        if not self.cookie:
            raise StopExperiment('missing_current_cookie')
        self.headers['rpc']['cookie'] = self.cookie
        self.headers['mediator']['cookie'] = self.cookie
        self.session = self.make_session()
        url = self.rpc['url']
        try:
            for stage in self.stages:
                if (self.root / 'STOP').exists():
                    raise StopExperiment('operator_stop_file')
                remove = omitted if scope in ('chain',stage) else None
                success, next_url = self.request(stage, url, remove)
                if not success:
                    return False, stage
                if stage != 'final':
                    url = initial_media_url(next_url) if stage == 'rpc' else next_url
            return True, ''
        finally:
            self.session.close()
            self.session = None
            self.last_case_end = time.monotonic()
            self.publish()

    def control(self):
        success, failed_stage = self.case()
        if not success:
            raise StopExperiment('positive_control_failed_' + failed_stage)

    def group(self, cases):
        rows = []
        for scope, field in cases:
            success, failed_stage = self.case(field, scope)
            row = {'scope':scope, 'removed_header':field, 'case_id':self.case_id,
                   'jpeg_verified':success, 'first_failed_stage':failed_stage,
                   'classification':'awaiting_control'}
            self.rows.append(row)
            rows.append(row)
            self.publish()
        self.control()
        for row in rows:
            row['classification'] = ('removal_passed_this_sample' if row['jpeg_verified']
                                     else 'removal_failed_between_healthy_controls')
        self.publish()

    def run(self):
        try:
            self.control()
            fields = list(self.fields)
            self.rng.shuffle(fields)
            if 'cookie' in fields:
                fields.remove('cookie')
                fields.insert(0,'cookie')
            for start in range(0,len(fields),self.args.group_size):
                self.group([('chain',field) for field in fields[start:start+self.args.group_size]])
            failed = [row['removed_header'] for row in self.rows
                      if row['scope']=='chain' and not row['jpeg_verified']]
            isolates = [(stage,field) for field in failed for stage in self.stages
                        if field in self.headers[stage]]
            for start in range(0,len(isolates),self.args.group_size):
                self.group(isolates[start:start+self.args.group_size])
            self.status = 'completed'
        except StopExperiment as exc:
            self.status = 'stopped_' + str(exc)
            for row in self.rows:
                if row['classification'] == 'awaiting_control':
                    row['classification'] = 'inconclusive_control_not_completed'
        except Exception as exc:
            self.status = 'stopped_exception_' + type(exc).__name__
            for row in self.rows:
                if row['classification'] == 'awaiting_control':
                    row['classification'] = 'inconclusive_control_not_completed'
        finally:
            if self.session is not None:
                self.session.close()
            self.publish()
            print(json.dumps({'status':self.status,'requests':self.seq,
                'completed_ablations':len(self.rows),'output':str(self.root)}),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rpc-input',required=True)
    parser.add_argument('--media-headers',required=True)
    parser.add_argument('--har',default='gemini.google.com3.har')
    parser.add_argument('--config',required=True)
    parser.add_argument('--package-root',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--interval',type=float,default=180)
    parser.add_argument('--group-size',type=int,default=1)
    parser.add_argument('--image-sha256',default='673a7253133214e00edd8421cb5261ddb17dd7f2f3ea65cc74a5c6c9a3adba39')
    args = parser.parse_args()
    if args.interval < 180:
        parser.error('case interval must be at least 180 seconds')
    if not 1 <= args.group_size <= 5:
        parser.error('group size must be 1..5')
    os.umask(0o077)
    import fcntl
    lock_dir = Path(args.output).parent.parent
    lock_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (lock_dir/'header-ablation.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another header ablation is already running')
        Experiment(args).run()


if __name__ == '__main__':
    main()
