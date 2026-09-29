# TRC analysis sandbox (trc-backend spec 2026-09-28 §5). No Hermes code, no secrets.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY deploy/trc/sandbox/requirements.txt /opt/sandbox/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /opt/sandbox/requirements.txt \
    && rm -rf /root/.cache

COPY deploy/trc/sandbox/runner.py deploy/trc/sandbox/launch.py /opt/sandbox/

CMD ["python", "-E", "-s", "-B", "/opt/sandbox/runner.py"]
