# Throwaway sandbox worker image — pandas only, no network tools needed at runtime.
# Built once on the Vultr VM; each job runs `docker run --rm --network none ...`.
FROM python:3.12-slim

RUN useradd --create-home --uid 65534 --shell /usr/sbin/nologin nobody 2>/dev/null || true \
 && pip install --no-cache-dir "pandas==2.2.3"

# No secrets baked in. Script + CSV arrive via bind-mount at /work.
WORKDIR /work
USER 65534:65534
ENTRYPOINT ["python"]
