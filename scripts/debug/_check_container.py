c = open('/app/app_fastapi.py', encoding='utf-8').read()
print('vue_index in file:', 'vue_index' in c)
print('header_uid[:128] in file:', 'header_uid[:128]' in c)
print('f"u:{" in file:', 'f"u:{' in c)
