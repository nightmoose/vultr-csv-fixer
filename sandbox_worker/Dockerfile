# Throwaway sandbox worker image — pandas only, no network tools needed at runtime.
# Built once on the Vultr VM; each job runs `docker run --rm --network none ...`.
FROM python:3.12-slim

RUN useradd --create-home --uid 65534 --shell /usr/sbin/nologin nobody 2>/dev/null || true \
 && pip install --no-cache-dir "pandas==2.2.3"

# No secrets baked in. Script + CSV arrive via bind-mount at /work.
# No entrypoint: the control plane already runs `python /work/fix.py`.
# An entrypoint of python would execute `python python /work/fix.py`
# and fail with "can't open file '/work/python'".
WORKDIR /work
USER 65534:65534
