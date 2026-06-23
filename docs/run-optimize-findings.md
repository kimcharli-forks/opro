# `mise run optimize` — Findings & Migration Notes

**Date:** 2026-06-23
**Goal:** Get `mise run optimize` (OPRO instruction optimization) running on this adaptation of the original Google OPRO repo.

This document records the chain of failures encountered, the fixes applied, the
API/model findings, and the runtime/cost analysis that led us to stop the full
run. It is a working log, not a polished design doc.

---

## TL;DR

`mise run optimize` is now **functional end-to-end** — both the scorer (Gemini)
and optimizer (OpenAI) server tests pass and the optimization loop runs. We
**stopped** the full run on purpose: at the default settings it would take
**multiple days** and cost **hundreds of dollars**. Use the smoke-test config
below for iteration.

Five distinct breakages were cleared, in order:

| # | Problem | Resolution |
|---|---------|-----------|
| 1 | `git push` blocked — GCP API key committed in history | Scrubbed key from history, force-rewrote unpushed commits, pushed |
| 2 | Run variables (`DATASET`, etc.) unset | Restored non-secret `[env]` to `.mise.toml` + wired `.mise.env` loading |
| 3 | Scorer used the **retired** PaLM `text-bison` / `generateText` API | Migrated to Gemini `generate_content` (`gemini-2.5-flash`) |
| 4 | Optimizer used **removed** `openai.ChatCompletion` API | Migrated to OpenAI v1 client (`openai>=1.0`, installed 2.43.0) |
| 5 | Optimizer returned **401** — `OPENAI_API_KEY` was a duplicate of the Google key | User regenerated a real `sk-...` key → 200 OK |

---

## 1. Push blocked by a committed secret

`git push` failed with GitHub Push Protection (`GH013`):

```
- GCP API Key Bound to a Service Account
    commit: 3e43c39  path: .mise.toml:10
    commit: 3e43c39  path: .mise.toml:11
```

- The key was hardcoded in `.mise.toml` (`PALM_API_KEY` / `OPENAI_API_KEY`, both
  set to the **same** value `AQ.Ab8…`) in commit `3e43c39` ("initial adaption in
  progress").
- The working tree was already clean (later commit switched to `${VAR}`
  references), but the secret was frozen in that commit's snapshot.
- All three unpushed commits (`3e43c39`, `c56e693`, `fe1e281`) were local-only,
  so a history rewrite was safe.

**Fix:** created backup branch `backup-before-secret-scrub`, then used
`git filter-branch --tree-filter` to replace the key with `${PALM_API_KEY}`
across the unpushed range, preserving the 3-commit structure. Verified `main`
was secret-free, then pushed (`a76bdce..0e978ec`).

> ⚠️ **Outstanding:** the exposed Google key still authenticates (it is a *live*
> credential). It must be **revoked/rotated** in the GCP console. The local
> `backup-before-secret-scrub` branch and `refs/original/` still contain the
> secret on disk — delete them once satisfied:
> ```bash
> git branch -D backup-before-secret-scrub
> git update-ref -d refs/original/refs/heads/main
> git reflog expire --expire=now --all && git gc --prune=now
> ```

## 2. Missing runtime variables

After the scrub, `.mise.toml` only *referenced* `${DATASET}`, `${OPTIMIZER_GPT}`,
etc., but nothing defined them → empty expansion → `AssertionError: dataset name
must be one of mmlu, bbh, or gsm8k`.

The `[env]` block that defined them had been removed along with the secret.

**Fix:**
- Restored the **non-secret** config to `.mise.toml` `[env]`:
  `OPTIMIZER_GPT=gpt-3.5-turbo`, `SCORER_TEXT=text-bison`,
  `INSTRUCTION_POS=Q_begin`, `DATASET=gsm8k`, `TASK=train`.
- Added `_.file = ".mise.env"` so mise loads the **secrets** (`PALM_API_KEY`,
  `OPENAI_API_KEY`) from the git-ignored `.mise.env` (already in `.gitignore`).

## 3. Scorer: PaLM `text-bison` is retired

`prompt_utils.call_palm_server_from_cloud` targeted `text-bison-001` via
`palm.generate_text()` (the legacy `generateText` method). It crashed with:

```
IndexError: list index out of range   # all_model_names[0]
```

**Diagnosis (probe with the live key):**
- The key authenticates fine — `palm.list_models()` returned **55 models**.
- **Zero** support the legacy `generateText` method. All are Gemini models
  (`generateContent`).
- Google has **fully retired** the PaLM `text-bison` API.

**Model availability probe** (current Gemini, via `google.generativeai` 0.x):

| Model | Result |
|-------|--------|
| `gemini-2.5-flash` | ✅ works (`'No.'`) — **chosen** |
| `gemini-flash-latest` | ✅ works |
| `gemini-2.5-flash-lite` | ✅ works (faster/cheaper option) |
| `gemini-2.0-flash` | ❌ 404 — "no longer available" (still *listed* by `list_models()`!) |
| `gemini-2.0-flash-001` | ❌ 404 — no longer available |

> Note: `list_models()` returns models that are no longer servable (e.g.
> `gemini-2.0-flash` lists but 404s on call). Don't trust the list alone.

**Fix:** rewrote `call_palm_server_from_cloud` to call Gemini via
`palm.GenerativeModel(model).generate_content(...)`, default
`model="gemini-2.5-flash"`, returning the same list-of-strings shape for
drop-in compatibility. Added bounded retry (5 attempts) and safe `.text`
access. Updated the scorer/optimizer `functools.partial` calls in
`optimize_instructions.py` from `model="text-bison-001"` →
`model="gemini-2.5-flash"`.

The `--scorer="text-bison"` flag is kept as the **logical selector** for the
"Google cloud model" path; it now routes to Gemini under the hood.

## 4. Optimizer: OpenAI SDK v1 migration

The OpenAI path used `openai.ChatCompletion.create` + `openai.error.*`, removed
in `openai>=1.0` (installed: **2.43.0**):

```
openai.lib._old_api.APIRemovedInV1: openai.ChatCompletion is no longer supported
```

**Fix in `prompt_utils.py`:**
- Added a lazy singleton `openai.OpenAI(...)` client (`_get_openai_client`),
  using `openai.api_key` if set else the `OPENAI_API_KEY` env var.
- `chat.completions.create(...)` instead of `ChatCompletion.create(...)`.
- Updated exception classes to v1: `APITimeoutError`, `RateLimitError`,
  `APIConnectionError`, `APIError` (ordered specific → base).
- **Fail-fast on auth errors:** `AuthenticationError` / `PermissionDeniedError`
  now raise immediately instead of being caught by the generic `APIError`
  retry — the old code would have looped forever on a 401.

## 5. Optimizer 401 — bad OpenAI key

First post-migration run returned `401 Unauthorized` from OpenAI. Cause:
`OPENAI_API_KEY` in `.mise.env` was **byte-identical** to the Google key
(`AQ.Ab8…`, 55 chars), not a real OpenAI key (those start with `sk-`).

**Fix:** user regenerated a real OpenAI key (`sk-proj-…`, 164 chars) → optimizer
test call returned `HTTP/1.1 200 OK`.

---

## Runtime & cost analysis (why we stopped)

Loop parameters for the gsm8k config (`optimize_instructions.py`):

- gsm8k train set = **7,473** rows (`data/gsm_data/gsm_train.tsv`)
- `train_ratio = 0.035` → train subset = `int(0.035 × 7473)` = **261** examples
- `num_generated_instructions_in_each_step = 8`
- `num_search_steps = 200`
- `evaluate_in_parallel = False` → **scoring is sequential**

**Per search step:** 8 instructions × 261 examples ≈ **2,088 sequential Gemini
scorer calls** (+ ~8 gpt-3.5 optimizer calls).

**Full run (200 steps):** ≈ **400,000+ Gemini calls** + ~1,600 OpenAI calls.

At ~1–2 s per sequential `gemini-2.5-flash` call (math prompts → thinking
tokens), that is on the order of **~5–10 days wall-clock** and **hundreds of
dollars** in combined Gemini + OpenAI usage. The original OPRO relied on
batched/parallel model serving that this adaptation does not replicate.

### Smoke-test config (recommended for iteration)

| Knob | File:line | Default | Test value |
|------|-----------|---------|------------|
| `num_search_steps` | `optimize_instructions.py:708` | 200 | **5** |
| `train_ratio` (gsm8k) | `optimize_instructions.py:644` | 0.035 (261 ex) | **0.005** (~37 ex) |
| `num_generated_instructions_in_each_step` | `optimize_instructions.py:707` | 8 | 4 |
| scorer model | `prompt_utils.py` | `gemini-2.5-flash` | `gemini-2.5-flash-lite` (faster) |

≈ 5 × 4 × 37 ≈ **740 calls** → minutes instead of days.

---

## Files changed in this session

- `.mise.toml` — restored non-secret `[env]` config; added `_.file = ".mise.env"`.
- `opro/prompt_utils.py` — Gemini scorer (`call_palm_server_from_cloud`); OpenAI
  v1 client + v1 exceptions + fail-fast auth handling.
- `opro/optimization/optimize_instructions.py` — scorer/optimizer partials point
  at `gemini-2.5-flash`.

These changes were **not committed** — left in the working tree for review.

## Open action items

1. **Revoke/rotate** the exposed Google key `AQ.Ab8…` (it still works).
2. Delete the local `backup-before-secret-scrub` branch + `refs/original` once
   the rewrite is confirmed good (commands in §1).
3. Decide whether to migrate the **optimizer** to Gemini too (removes the OpenAI
   dependency entirely) or keep gpt-3.5-turbo.
4. Consider adding **parallel evaluation** (`evaluate_in_parallel`) and/or
   migrating off the deprecated `google.generativeai` package to `google.genai`
   before any large run.
5. Keys are passed as **CLI flags** by the mise task, so they appear in plaintext
   in the process list (`ps`). Consider reading them from env inside the script
   instead.
