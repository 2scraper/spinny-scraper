# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium — for a CI canary run or a scheduled job,
# not required for local development (`pip install` directly is simpler there).
#
#   docker build -t spinny-scraper .
#   docker run --rm -v "$PWD/out:/out" spinny-scraper \
#     --url "https://www.spinny.com/used-cars-in-delhi-ncr/s/" \
#     --pages 5 --out /out/delhi
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

# Chromium goes in a path every user can read. Playwright's default is under
# the INSTALLING user's home, so the non-root user below would not find it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# libatomic1 BEFORE the pip/playwright layer: Playwright 1.65 (2026-11-09)
# bundles a node that needs libatomic.so.1, which python:3.12-slim (Debian 13)
# lacks, and the `playwright` CLI itself runs on that node — so
# `playwright install --with-deps` dies (exit 127) before it could add it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-playwright.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: an earlier version omitted
# proxy_pool.py, which the engine imports at module level, so the image died
# with ModuleNotFoundError on every invocation INCLUDING `--help` — a broken
# container that nothing in the repo would have noticed.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py ./

# Run as an unprivileged user: a scraper parses untrusted pages in a browser,
# and nothing here needs root once Chromium is installed. Mount the output
# directory writable for UID 10001 (or pass `--user "$(id -u)"`).
RUN useradd --create-home --uid 10001 scraper \
    && mkdir -p /out && chown scraper /out
USER scraper

ENTRYPOINT ["python3", "playwright_scraper.py"]
CMD ["--help"]
