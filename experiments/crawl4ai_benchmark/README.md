# Crawl4AI benchmark

Temporary evaluation infrastructure for comparing Crawl4AI with the existing Phase 3 contact extractor. It is not part of the production application and does not write to SQLite.

The benchmark uses an isolated Python environment and the fixed ten-page sample in `benchmark.py`. It checks robots rules, runs sequentially with a one-second delay, uses ordinary headless Chromium without stealth/proxies, and never guesses contact data.

Installation used for the recorded run:

```bash
uv venv /tmp/crawl4ai-benchmark-venv --python 3.12
uv pip install --python /tmp/crawl4ai-benchmark-venv/bin/python 'crawl4ai==0.9.4'
uv pip install --python /tmp/crawl4ai-benchmark-venv/bin/python -e .
/tmp/crawl4ai-benchmark-venv/bin/crawl4ai-setup
/tmp/crawl4ai-benchmark-venv/bin/crawl4ai-doctor
PYTHONPATH=src /tmp/crawl4ai-benchmark-venv/bin/python experiments/crawl4ai_benchmark/benchmark.py
```

The official setup installed Playwright Chromium, Chromium Headless Shell, FFmpeg, and the equivalent Patchright browser bundle. Patchright/stealth features were not used.
