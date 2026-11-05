#!/usr/bin/env python3
"""Publish a signed, notarized AVE Studio DMG as an update: the appcast entry, then both files to the private R2 bucket.

    scripts/publish_update.py --dmg out/AVE-Studio-0.2.0-arm64.dmg --version 0.2.0 --build 20261110 --dry-run
    scripts/publish_update.py --dmg ... --version 0.2.0 --build 20261110          # uploads (a human act)

What it does, in order:
  1. checks the DMG (exists; stapled notarization ticket unless --skip-notarization-check);
  2. signs it with Sparkle's `sign_update`, which reads the EdDSA PRIVATE key itself from the login Keychain item that
     Sparkle's `generate_keys --account ave-studio` made (service "https://sparkle-project.org", account "ave-studio").
     This script never sees, prints or stores that key;
  3. writes appcast.xml (one item: sparkle:version = --build, shortVersionString = --version, minimumSystemVersion,
     enclosure url/length/edSignature), signs the feed itself with `sign_update` (the app requires a signed feed), and
     reads it back with ave.updates to check that it offers this build;
  4. uploads the DMG, then the appcast (so the feed never names a file that is not there yet), with an S3-compatible
     SigV4 PUT to Cloudflare R2.

Settings are local, never in the repo: ~/.config/ave/updates.json {"feed_url", "r2_endpoint", "r2_bucket"} or
AVE_UPDATE_FEED_URL, AVE_R2_ENDPOINT, AVE_R2_BUCKET. R2 credentials: AVE_R2_ACCESS_KEY_ID / AVE_R2_SECRET_ACCESS_KEY, else
the Keychain items service "ave-update-r2", accounts "access-key-id" and "secret-access-key" (read only).
The feed address is private: everything printed has it replaced by "[update feed]". `--dry-run` does all of the above
except fetching credentials and uploading, and prints what it would upload.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import re
import subprocess
import sys
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit
from xml.sax.saxutils import escape, quoteattr

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from ave import updates  # noqa: E402

SPARKLE_NS = updates.SPARKLE_NS
KEY_ACCOUNT = 'ave-studio'                        # generate_keys / sign_update --account
KEYCHAIN_SERVICE = 'ave-update-r2'
DEFAULT_SIGNER = Path.home() / 'Library/Caches/AVE/sparkle/Sparkle-2.10.0/bin/sign_update'
MIN_SYSTEM = '14.0'


class PublishError(SystemExit):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------------- signing
_ED = re.compile(r'sparkle:edSignature="([^"]+)"')
_LEN = re.compile(r'length="(\d+)"')


def sign_archive(signer: Path, dmg: Path, account: str = KEY_ACCOUNT) -> tuple[str, int]:
    """(edSignature, length) from Sparkle's sign_update; the private key stays in the Keychain."""
    run = subprocess.run([str(signer), '--account', account, str(dmg)], capture_output=True, text=True)
    if run.returncode != 0:
        raise PublishError(f'sign_update failed ({run.returncode}): {updates.redact(run.stderr.strip())[:300]}')
    sig, length = _ED.search(run.stdout), _LEN.search(run.stdout)
    if not sig or not length:
        raise PublishError('sign_update printed no signature')
    if int(length.group(1)) != dmg.stat().st_size:
        raise PublishError('sign_update signed a different length than the DMG has')
    return sig.group(1), int(length.group(1))


def sign_feed(signer: Path, feed: Path, account: str = KEY_ACCOUNT) -> None:
    """sign_update embeds the feed's own signature in the XML file (Info.plist SURequireSignedFeed)."""
    before = feed.read_bytes()
    run = subprocess.run([str(signer), '--account', account, str(feed)], capture_output=True, text=True)
    if run.returncode != 0:
        raise PublishError(f'sign_update could not sign the appcast ({run.returncode}): {updates.redact(run.stderr.strip())[:300]}')
    if feed.read_bytes() == before:
        raise PublishError('sign_update left the appcast unsigned')


# ----------------------------------------------------------------------------------------------------------- appcast
def appcast(*, version: str, build: str, url: str, length: int, signature: str, minimum_system: str, when: dt.datetime) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="{SPARKLE_NS}">
  <channel>
    <title>AVE Studio</title>
    <item>
      <title>AVE Studio {escape(version)}</title>
      <pubDate>{format_datetime(when)}</pubDate>
      <sparkle:version>{escape(build)}</sparkle:version>
      <sparkle:shortVersionString>{escape(version)}</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>{escape(minimum_system)}</sparkle:minimumSystemVersion>
      <enclosure url={quoteattr(url)} length="{length}" type="application/octet-stream" sparkle:edSignature={quoteattr(signature)}/>
    </item>
  </channel>
</rss>
'''


# ------------------------------------------------------------------------------------------------------------ S3 / R2
def _h(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4(method: str, url: str, headers: dict, *, access_key: str, secret_key: str, region: str, payload_sha256: str,
          amz_date: str, service: str = 's3') -> dict:
    """AWS Signature Version 4 headers for one request (what R2's S3 API accepts). Returns the headers to send."""
    p = urlsplit(url)
    all_headers = {k.lower(): str(v) for k, v in headers.items()}
    all_headers.update({'host': p.netloc, 'x-amz-content-sha256': payload_sha256, 'x-amz-date': amz_date})
    signed = ';'.join(sorted(all_headers))
    canonical_headers = ''.join(f'{k}:{" ".join(all_headers[k].strip().split())}\n' for k in sorted(all_headers))
    query = '&'.join(sorted(f'{quote(k, safe="-_.~")}={quote(v, safe="-_.~")}' for k, v in parse_qsl(p.query, keep_blank_values=True)))
    request = '\n'.join([method, quote(p.path or '/', safe='/-_.~'), query, canonical_headers, signed, payload_sha256])
    day = amz_date[:8]
    scope = f'{day}/{region}/{service}/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, _h(request)])
    key = _hmac(_hmac(_hmac(_hmac(('AWS4' + secret_key).encode(), day), region), service), 'aws4_request')
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers['authorization'] = f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}'
    return all_headers


def keychain_secret(account: str) -> str | None:
    run = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', account, '-w'],
                         capture_output=True, text=True)
    return (run.stdout.strip() or None) if run.returncode == 0 else None


def credentials() -> tuple[str, str]:
    key = os.environ.get('AVE_R2_ACCESS_KEY_ID') or keychain_secret('access-key-id')
    secret = os.environ.get('AVE_R2_SECRET_ACCESS_KEY') or keychain_secret('secret-access-key')
    if not key or not secret:
        raise PublishError(f'No R2 credentials: set AVE_R2_ACCESS_KEY_ID and AVE_R2_SECRET_ACCESS_KEY, or add Keychain items '
                           f'service "{KEYCHAIN_SERVICE}" accounts "access-key-id" and "secret-access-key".')
    return key, secret


def put(endpoint: str, bucket: str, key: str, path: Path, content_type: str, cache_control: str, creds: tuple[str, str],
        feed_url: str) -> None:
    url = f'{endpoint.rstrip("/")}/{bucket}/{key}'
    p = urlsplit(url)
    if p.scheme != 'https':
        raise PublishError('The R2 endpoint must be https://')
    digest = sha256_file(path)
    amz_date = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    headers = sigv4('PUT', url, {'content-type': content_type, 'cache-control': cache_control, 'content-length': str(path.stat().st_size)},
                    access_key=creds[0], secret_key=creds[1], region='auto', payload_sha256=digest, amz_date=amz_date)
    headers.pop('host')
    conn = http.client.HTTPSConnection(p.netloc, timeout=600)
    try:
        with path.open('rb') as body:
            conn.request('PUT', quote(p.path, safe='/-_.~'), body=body, headers=headers)
            r = conn.getresponse()
            text = r.read(2000).decode(errors='replace')
    except Exception as exc:  # noqa: BLE001 - OSError, http.client.HTTPException, anything an S3 client raises: all redacted
        raise PublishError(f'Upload of {updates.redact(key, feed_url)} failed: '
                           + updates.redact(f'{type(exc).__name__}: {exc}', feed_url)[:300]) from None
    finally:
        conn.close()
    if r.status not in (200, 201):
        raise PublishError(f'Upload of {updates.redact(key, feed_url)} failed: HTTP {r.status} {updates.redact(text, feed_url)[:300]}')


# --------------------------------------------------------------------------------------------------------------- main
def plan_keys(feed_url: str, dmg_name: str) -> tuple[str, str, str]:
    """(appcast key, dmg key, dmg url) under the feed's own private prefix."""
    p = urlsplit(feed_url)
    if p.scheme != 'https' or not p.path.endswith('.xml') or p.query or p.fragment:
        raise PublishError('The feed address must be https://<host>/<private prefix>/appcast.xml')
    feed_key = p.path.lstrip('/')
    prefix = feed_key.rsplit('/', 1)[0] if '/' in feed_key else ''
    dmg_key = (prefix + '/' if prefix else '') + dmg_name
    return feed_key, dmg_key, feed_url.rsplit('/', 1)[0] + '/' + quote(dmg_name)


def main(argv=None) -> int:
    """Every failure ends as one redacted line, never a traceback that could carry the private prefix."""
    try:
        return _main(argv)
    except PublishError as exc:
        raise PublishError(updates.redact(str(exc.code))) from None
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PublishError('publish_update failed: ' + updates.redact(f'{type(exc).__name__}: {exc}')[:500]) from None


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dmg', type=Path, required=True)
    ap.add_argument('--version', required=True, help='CFBundleShortVersionString of the app inside, e.g. 0.2.0')
    ap.add_argument('--build', required=True, help='CFBundleVersion of the app inside (the appcast sparkle:version)')
    ap.add_argument('--minimum-system', default=MIN_SYSTEM)
    ap.add_argument('--sign-update', type=Path, default=DEFAULT_SIGNER, help="Sparkle's bin/sign_update")
    ap.add_argument('--key-account', default=KEY_ACCOUNT)
    ap.add_argument('--out', type=Path, help='where to write appcast.xml (default: a new private 0700 temporary folder)')
    ap.add_argument('--dry-run', action='store_true', help='everything except fetching credentials and uploading')
    ap.add_argument('--skip-notarization-check', action='store_true', help='do not require a stapled ticket (tests only)')
    a = ap.parse_args(argv)

    cfg = updates.local_config()
    feed_url = cfg.get('feed_url')
    if not feed_url:
        raise PublishError(f'No update feed address: set {updates.FEED_ENV} or "feed_url" in {updates.CONFIG}')
    endpoint, bucket = cfg.get('r2_endpoint'), cfg.get('r2_bucket')
    if not a.dry_run and (not endpoint or not bucket):
        raise PublishError(f'Set r2_endpoint (https://<account id>.r2.cloudflarestorage.com) and r2_bucket in {updates.CONFIG}')
    dmg = a.dmg.absolute()
    if not dmg.is_file() or dmg.suffix != '.dmg':
        raise PublishError(f'{dmg} is not a DMG')
    if not re.fullmatch(r'\d+(\.\d+)*', a.build):
        raise PublishError('--build is digits and dots, the CFBundleVersion of the app in the DMG')
    if not a.skip_notarization_check:
        check = subprocess.run(['/usr/bin/xcrun', 'stapler', 'validate', str(dmg)], capture_output=True, text=True)
        if check.returncode != 0:
            raise PublishError('The DMG has no stapled notarization ticket: run scripts/sign_desktop.py with --notary-profile first')
    if not a.sign_update.is_file():
        raise PublishError(f'No sign_update at {a.sign_update}: build_desktop.py unpacks Sparkle there, or pass --sign-update')

    feed_key, dmg_key, dmg_url = plan_keys(feed_url, dmg.name)
    signature, length = sign_archive(a.sign_update, dmg, a.key_account)
    if a.out:
        out = a.out.absolute()
        out.mkdir(parents=True, exist_ok=True)
    else:                                  # the appcast names the private address: keep it out of shared or committed folders
        import tempfile
        out = Path(tempfile.mkdtemp(prefix='ave-appcast-'))
    feed = out / 'appcast.xml'
    feed.write_text(appcast(version=a.version, build=a.build, url=dmg_url, length=length, signature=signature,
                            minimum_system=a.minimum_system, when=dt.datetime.now(dt.timezone.utc)))
    sign_feed(a.sign_update, feed, a.key_account)
    offered = updates.check(feed_url, current='0', system=a.minimum_system, fetch=lambda _url: feed.read_bytes())
    if offered is None or offered.version != a.build or offered.length != dmg.stat().st_size:
        raise PublishError('The appcast does not offer this build; nothing uploaded')

    uploads = [
        {'key': dmg_key, 'path': dmg, 'content_type': 'application/x-apple-diskimage', 'cache_control': 'public, max-age=31536000, immutable'},
        {'key': feed_key, 'path': feed, 'content_type': 'application/xml', 'cache_control': 'no-cache, max-age=0'},
    ]
    prefix = feed_key.rsplit('/', 1)[0] + '/' if '/' in feed_key else ''
    def hidden(key):
        return updates.REDACTED + '/' + key[len(prefix):] if prefix and key.startswith(prefix) else key
    shown = [{'key': hidden(u['key']), 'file': str(u['path']), 'bytes': u['path'].stat().st_size,
              'sha256': sha256_file(u['path']), 'content_type': u['content_type'], 'cache_control': u['cache_control']} for u in uploads]
    report = {'dry_run': a.dry_run, 'version': a.version, 'build': a.build, 'minimum_system': a.minimum_system,
              'bucket': bucket or '(not set)', 'endpoint': 'configured' if endpoint else '(not set)',
              'feed': updates.redact(feed_url, feed_url), 'enclosure': updates.redact(dmg_url, feed_url),
              'ed_signature': signature, 'appcast': str(feed), 'uploads': shown}
    if a.dry_run:
        print(json.dumps(report, indent=2))
        print('Dry run: nothing was uploaded.', file=sys.stderr)
        return 0
    creds = credentials()
    for u in uploads:
        put(endpoint, bucket, u['key'], u['path'], u['content_type'], u['cache_control'], creds, feed_url)
    report['uploaded'] = True
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
