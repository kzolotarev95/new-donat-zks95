#!/usr/bin/env python3
"""Upgrade project proxy locations while preserving TLS and other services."""
from pathlib import Path
import re
import sys


def add_read_retry(text, port):
    if 'location @zks95_read_retry' in text:
        return text
    health = re.search(r'(?m)^([ \t]*)location[ \t]+=[ \t]*/healthz[ \t]*\{', text)
    roots = list(re.finditer(r'(?m)^([ \t]*)location[ \t]+/[ \t]*\{[ \t]*$', text[:health.start()])) if health else []
    if not roots:
        raise ValueError('Cannot safely add read retries: expected project locations are missing')
    root = roots[-1]
    indent = root[1]
    retry = f'''{indent}location @zks95_read_retry {{
{indent}    proxy_pass http://127.0.0.1:{int(port)};
{indent}    proxy_http_version 1.1;
{indent}    proxy_set_header Host $http_host;
{indent}    proxy_set_header X-Real-IP $remote_addr;
{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
{indent}    proxy_set_header X-Forwarded-Proto $scheme;
{indent}    proxy_read_timeout 60s;
{indent}}}

'''
    text = text[:health.start()] + retry + text[health.start():]
    injection = f'''
{indent}    # Retry transport errors for reads only; never replay a payment POST.
{indent}    if ($request_method ~ ^(GET|HEAD)$) {{
{indent}        error_page 502 504 = @zks95_read_retry;
{indent}    }}'''
    return text[:root.end()] + injection + text[root.end():]


def preserve_public_host(text, port):
    """Pass the original Host authority, including :8443, to this backend."""
    backend = re.compile(r'(?m)^([ \t]*)proxy_pass[ \t]+http://127\.0\.0\.1:'
                         + re.escape(str(int(port))) + r'(?:/healthz)?[ \t]*;[^\n]*$')
    location = re.compile(r'(?m)^([ \t]*)location[^\n{]*\{[ \t]*$')
    host_header = re.compile(r'(?m)^([ \t]*proxy_set_header[ \t]+Host[ \t]+)\$host([ \t]*;)')
    changes = []
    for start in location.finditer(text):
        end = re.search(r'(?m)^' + re.escape(start[1]) + r'\}[ \t]*(?:#[^\n]*)?$', text[start.end():])
        if end is None:
            continue
        stop = start.end() + end.end()
        block = text[start.start():stop]
        upstream = backend.search(block)
        if upstream is None:
            continue
        updated = host_header.sub(r'\1$http_host\2', block)
        if not re.search(r'(?m)^[ \t]*proxy_set_header[ \t]+Host[ \t]+', updated):
            # Older project configurations may omit Host entirely.
            updated = updated[:upstream.end()] + f'\n{upstream[1]}proxy_set_header Host $http_host;' + updated[upstream.end():]
        changes.append((start.start(), stop, updated))
    for start, stop, updated in reversed(changes):
        text = text[:start] + updated + text[stop:]
    return text


def upgrade_config(text, port):
    return preserve_public_host(add_read_retry(text, port), port)


if __name__ == '__main__':
    path = Path(sys.argv[1])
    path.write_text(upgrade_config(path.read_text(), sys.argv[2]))
