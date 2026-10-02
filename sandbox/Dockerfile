# Fullhouse Hackathon — Bot Sandbox Container
# One container per bot per match. Stateless, disposable.
#
# Build from the repo root (the runner lives in arena/runner/):
#   docker build -f sandbox/Dockerfile -t poker-harness-sandbox:latest .
#   (or ./sandbox.sh build)
#
# Security properties enforced at runtime (see SubprocessBotSeat._command
# in arena/seats.py):
#   --network none                       no internet access
#   --memory 768m                        OOM-killed if exceeded
#   --cpus 0.5                           half a core max
#   --read-only                          filesystem is read-only
#   --security-opt no-new-privileges     can't escalate permissions
#   --user 1000:1000                     non-root

# Python 3.10 (not 3.11+) — eval7 0.1.7's pre-generated C files reference
# longintrepr.h, which Python 3.11 moved from Include/ to Include/cpython/.
# eval7 was last released for 3.7-3.10; building on 3.11+ fails with
# 'longintrepr.h: No such file or directory'. 3.10 is fully supported by
# every other dep we ship (numpy, scipy, treys, sklearn).
FROM python:3.10-slim

# Install only what bots are allowed to use.
# Participants can request additions via [library-request] GitHub issues — we
# vet and rebuild the image once before D1.
#
# eval7 quirk: its setup.py imports Cython at the top, so pip's default build
# isolation (which spins up an empty venv) breaks the install. Two-step:
# install Cython<3 into the actual build env, then install eval7 with
# --no-build-isolation so it can see Cython. Everything else after that
# installs the normal way.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && pip install --no-cache-dir "Cython<3" \
 && pip install --no-cache-dir --no-build-isolation eval7==0.1.7 \
 && pip install --no-cache-dir \
    numpy==1.26.4 \
    scipy==1.13.0 \
    treys==0.1.8 \
    scikit-learn==1.5.2 \
 && apt-get purge -y --auto-remove gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -u 1000 -m botuser

# Bot runner (protocol v2, stdlib only) at a fixed path
COPY arena/runner/bot_runner.py /runner.py
RUN chmod +r /runner.py

# Bot code is mounted at runtime: -v /path/to/botdir:/bot:ro
# We do NOT copy bot code into the image — each bot gets its own volume mount

USER botuser
WORKDIR /home/botuser

# stdin/stdout carry the protocol (see the runner's docstring)
CMD ["python3", "-u", "/runner.py"]
