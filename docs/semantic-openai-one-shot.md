# One-shot Lagoon semantic analysis

The local runner fetches the existing bounded first-party sample, classifies and deduplicates facts, counts the complete OpenAI request, and writes a preparation artifact. It has no database write path. The optional tokenizer dependency is listed in `requirements-semantic.txt`; the provider uses the application's existing `httpx` dependency and performs one HTTPS request with no SDK or retry middleware.

Run from this worktree after installing the tokenizer:

```sh
python3 -m pip install -r requirements-semantic.txt
read -s "OPENAI_API_KEY?Enter the existing API key (hidden): "
export OPENAI_API_KEY
PYTHONPATH=src python3 scripts/run_lagoon_semantic_analysis.py --execute
unset OPENAI_API_KEY
```

Enter the existing credential in the hidden terminal prompt; do not put it in a command argument, project file, or chat. If it is stored in a local secret manager, inject it into the same process environment instead. The runner blocks unless the exact model tokenizer is available, the full request is at most 15,000 estimated tokens including a 256-token framing margin, and the worst-case cost is below USD 0.05. It writes a one-shot marker immediately before its sole network request. A failure after that point is preserved and cannot be retried through this runner.

The output artifact path is `artifacts/semantic-poc/lagoon-openai-run.json`. A result can only be called successful when it contains the actual Responses API ID and usage. The application's deterministic checks establish response shape, first-party provenance, known evidence IDs, and numeric/comparative claim checks; they do not establish semantic entailment or independently verify the model's conclusions. All generated claims remain human-review items. The script creates only a non-sendable preview and never persists an invitation or sends email.
