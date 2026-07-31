"""Check frontend serving."""
import urllib.request, re

# Check root page
resp = urllib.request.urlopen('http://localhost:7860/', timeout=5)
body = resp.read().decode('utf-8')
print(f'Content-Type: {resp.headers.get("Content-Type")}')
print(f'Status: {resp.status}')

# Find script tags
for m in re.finditer(r'src=["\']([^"\']+)["\']', body):
    print(f'  Script src: {m.group(1)}')
for m in re.finditer(r'href=["\']([^"\']+)["\']', body):
    href = m.group(1)
    if href.endswith('.css') or 'favicon' in href:
        print(f'  Link href: {href}')

# Try loading a script
print('\n--- Checking static assets ---')
for asset in ['/assets/index.js', '/assets/index.css']:
    try:
        resp = urllib.request.urlopen(f'http://localhost:7860{asset}', timeout=5)
        print(f'{asset}: {resp.status} ({len(resp.read())} bytes)')
    except Exception as e:
        print(f'{asset}: {e}')
