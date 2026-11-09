#!/usr/bin/env python3
"""Add one transport retry for reads to the existing project virtual host."""
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
{indent}    proxy_set_header Host $host;
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


if __name__ == '__main__':
    path = Path(sys.argv[1])
    path.write_text(add_read_retry(path.read_text(), sys.argv[2]))
