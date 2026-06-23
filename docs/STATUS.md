# Project Status — resume here

**Last updated:** 2026-06-23
**Branch:** `main` (pushed) · **Latest commit:** `9b320bd`

A running snapshot so work can be picked up next session. For *how it works* see
[`how-it-works.md`](./how-it-works.md); for *usage* see [`README.md`](./README.md);
for the *full migration log* see [`run-optimize-findings.md`](./run-optimize-findings.md).

---

## Where things stand

`mise run optimize` (cloud) and `mise run optimize-local` (local LLM) both work
end-to-end. Verified: a local run drives `Qwen3.6-27B-OptiQ-4bit` on the Apple
Silicon **GPU** (oMLX/Metal) via an OpenAI-compatible server — no cloud calls.

### Done
- [x] Push-protection secret removed from git history; pushed.
- [x] Scorer migrated off retired PaLM `text-bison` → **Gemini** `gemini-2.5-flash`.
- [x] OpenAI calls migrated to the **v1 SDK** (+ fail-fast on auth errors).
- [x] `evaluate` task added (mirrors `optimize`).
- [x] **Local backend**: `optimize-local` / `evaluate-local` route both roles to a
      local OpenAI-compatible server (`OPENAI_BASE_URL` + `OPENAI_MODEL_OVERRIDE`
      + `OPENAI_DISABLE_THINKING`).
- [x] Deprecated `google-generativeai` → **`google-genai`** SDK.
- [x] Docs: `README.md`, `how-it-works.md`, `run-optimize-findings.md`.
- [x] Smoke run confirmed local + GPU + scorer + optimizer all functioning.

### Current configuration (heads-up)
- ⚠️ **`optimize_instructions.py` ships with SMOKE sizing** — `train_ratio=0.003`
  (line 643), `num_generated_instructions_in_each_step=3` (706),
  `num_search_steps=3` (707). Originals are in the inline `# SMOKE TEST (was …)`
  comments. **Restore them for any real run.**
- **`.mise.env` is in LOCAL mode** (git-ignored). It has `OPENAI_BASE_URL` →
  local server, `OPENAI_MODEL_OVERRIDE=Qwen3.6-27B-OptiQ-4bit`,
  `OPENAI_DISABLE_THINKING=1`, and `OPENAI_API_KEY` = the **local server key**.
  The real cloud OpenAI `sk-proj-…` key was overwritten and is no longer there.
- `PALM_API_KEY` in `.mise.env` is still the **exposed** key `AQ.Ab8…` (it works
  for Gemini, but should be revoked).

---

## Open action items (next session)

1. **Revoke/rotate** the exposed Google key `AQ.Ab8…` (still live).
2. **Restore full sizing** in `optimize_instructions.py` before any real run
   (or make these env/flag-configurable instead of hardcoded).
3. **Clean up local secret refs** from the history rewrite:
   ```bash
   git branch -D backup-before-secret-scrub
   git update-ref -d refs/original/refs/heads/main
   git reflog expire --expire=now --all && git gc --prune=now
   ```
4. If cloud mode is wanted again: re-add the real OpenAI `sk-proj-…` key to
   `.mise.env` and remove the `OPENAI_BASE_URL` line (it globally redirects all
   OpenAI-path calls to local while set).
5. Performance ideas for a full local run: enable parallel evaluation
   (`evaluate_in_parallel`) and/or use a faster scorer model — local scoring is
   ~**15 s/call** (the GSM8K scorer writes a full solution each time).

---

## How to resume

### Run locally (current default)
```bash
# 1. Ensure the local server is up with the model loaded:
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" -w "\n%{http_code}\n"
# 2. Run (smoke-sized as committed):
mise run optimize-local      # or evaluate-local
```

### Run against the cloud
```bash
# Needs PALM_API_KEY (Gemini) + a real OPENAI_API_KEY (sk-...) in .mise.env,
# and the OPENAI_BASE_URL line removed.
mise run optimize            # or evaluate
```

### Verify it's local + on the GPU
See the command block in [`how-it-works.md`](./how-it-works.md#operational-qa-with-commands)
(`pgrep`, `lsof -iTCP:8000`, `ioreg … IOAccelerator`).

---

## Key facts to remember
- Local server: `http://127.0.0.1:8000/v1`, **oMLX** (Apple Silicon Metal GPU),
  multi-model (routes by `model` id), API key lives in `.mise.env`.
- Both roles use **one** model (`Qwen3.6-27B-OptiQ-4bit`, thinking off) — an MLX
  server reloads when the model id changes, so keep it constant.
- Dotenv gotcha: **single-quote** keys containing `$` in `.mise.env` (mise
  expands `$NAME`).
- Full GSM8K run ≈ 400k scorer calls ≈ days locally — always size down for tests.
