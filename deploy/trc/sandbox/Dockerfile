# TRC analysis sandbox (trc-backend spec 2026-09-28 §5). No Hermes code, no secrets.
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY deploy/trc/sandbox/requirements.txt /opt/sandbox/requirements.txt
# No pip at runtime (spec §5.1). setuptools/wheel are uninstalled only if the base image
# has them (pip skips an absent package); pip goes last, in its own call.
RUN pip install --no-cache-dir --require-hashes -r /opt/sandbox/requirements.txt \
    && python -m pip uninstall -y setuptools wheel \
    && python -m pip uninstall -y pip \
    && rm -rf /root/.cache

# Mount points for the per-slot tmpfs (compose), on the read-only root.
RUN mkdir -p /work/slot-20001 /work/slot-20002 /work/slot-20003 \
    && chmod 0755 /work \
    && chmod 0700 /work/slot-20001 /work/slot-20002 /work/slot-20003

COPY deploy/trc/sandbox/runner.py deploy/trc/sandbox/launch.py /opt/sandbox/

CMD ["python", "-E", "-s", "-B", "/opt/sandbox/runner.py"]
