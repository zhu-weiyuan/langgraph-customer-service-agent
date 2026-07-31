"""Check docker mounts."""
import json, subprocess

result = subprocess.run(
    ['docker', 'inspect', 'langgraph-cs-agent', '--format', '{{json .Mounts}}'],
    capture_output=True, text=True
)
if result.returncode == 0:
    data = json.loads(result.stdout.strip())
    for m in data:
        print(f'{m.get("Source", "?")} -> {m.get("Destination", "?")} ({"RO" if m.get("Mode","") == "ro" else "RW"})')
else:
    print(f'Error: {result.stderr}')
