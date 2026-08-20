import sys
content = open('/app/app_fastapi.py').read()
if 'f"u:{header_uid}"' in content:
    print('FAIL: old prefix')
    sys.exit(1)
elif 'header_uid[:128]' in content:
    print('OK: patched')
else:
    print('UNKNOWN')
