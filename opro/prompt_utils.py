# Copyright 2023 The OPRO Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The utility functions for prompting GPT and Google Cloud models."""

import os
import time
from google import genai
from google.genai import types
import openai


_genai_client = None
_genai_api_key = None


def configure_genai(api_key):
  """Store the Gemini API key for the lazily-created google-genai client.

  Replaces the old global ``palm.configure(api_key=...)``. The client is
  rebuilt on the next call so a newly provided key takes effect.
  """
  global _genai_api_key, _genai_client
  _genai_api_key = api_key
  _genai_client = None


def _get_genai_client():
  """Lazily build a singleton google-genai client.

  Uses the key from :func:`configure_genai` if set; otherwise the SDK falls
  back to the ``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` environment variables.
  """
  global _genai_client
  if _genai_client is None:
    _genai_client = (
        genai.Client(api_key=_genai_api_key)
        if _genai_api_key
        else genai.Client()
    )
  return _genai_client


_openai_client = None


def _get_openai_client():
  """Lazily build a singleton OpenAI v1 client.

  Uses ``openai.api_key`` if the caller set it (as optimize_instructions.py
  does from the ``--openai_api_key`` flag); otherwise the client falls back to
  the ``OPENAI_API_KEY`` environment variable.

  The base URL is read from the ``OPENAI_BASE_URL`` environment variable by the
  SDK, so pointing it at a local OpenAI-compatible server (e.g. an MLX/LM Studio
  endpoint at http://127.0.0.1:8000/v1) requires no code change here.
  """
  global _openai_client
  if _openai_client is None:
    _openai_client = openai.OpenAI(api_key=getattr(openai, "api_key", None))
  return _openai_client


def call_openai_server_single_prompt(
    prompt, model="gpt-3.5-turbo", max_decode_steps=20, temperature=0.8
):
  """The function to call OpenAI (or an OpenAI-compatible server) with a prompt.

  If ``OPENAI_MODEL_OVERRIDE`` is set, it replaces ``model`` for the actual API
  call. This lets the OPRO scripts keep using the logical name ``gpt-3.5-turbo``
  (so all the GPT-path parsing logic stays valid) while the request is served by
  whatever model a local endpoint has loaded, e.g. ``Qwen3.6-27B-OptiQ-4bit``.

  If ``OPENAI_DISABLE_THINKING`` is truthy, the request asks the server to turn
  off "thinking" tokens (``chat_template_kwargs={"enable_thinking": False}``),
  which makes reasoning models like Qwen3 answer in ~1 token instead of
  hundreds — essential for the high-volume scorer role. This extra field is only
  sent when the flag is set, since the hosted OpenAI API rejects unknown params.
  """
  model = os.environ.get("OPENAI_MODEL_OVERRIDE") or model
  extra_kwargs = {}
  if os.environ.get("OPENAI_DISABLE_THINKING"):
    extra_kwargs["extra_body"] = {
        "chat_template_kwargs": {"enable_thinking": False}
    }
  try:
    completion = _get_openai_client().chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_decode_steps,
        messages=[
            {"role": "user", "content": prompt},
        ],
        **extra_kwargs,
    )
    return completion.choices[0].message.content

  except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
    # Bad/expired key or insufficient permissions — retrying cannot help, so
    # fail fast instead of looping forever.
    raise RuntimeError(
        f"OpenAI authentication failed ({e}). Check OPENAI_API_KEY."
    ) from e

  except openai.APITimeoutError as e:
    retry_time = getattr(e, "retry_after", None) or 30
    print(f"Timeout error occurred. Retrying in {retry_time} seconds...")
    time.sleep(retry_time)
    return call_openai_server_single_prompt(
        prompt, model=model, max_decode_steps=max_decode_steps,
        temperature=temperature
    )

  except openai.RateLimitError as e:
    retry_time = getattr(e, "retry_after", None) or 30
    print(f"Rate limit exceeded. Retrying in {retry_time} seconds...")
    time.sleep(retry_time)
    return call_openai_server_single_prompt(
        prompt, model=model, max_decode_steps=max_decode_steps,
        temperature=temperature
    )

  except openai.APIConnectionError as e:
    retry_time = getattr(e, "retry_after", None) or 30
    print(f"API connection error occurred. Retrying in {retry_time} seconds...")
    time.sleep(retry_time)
    return call_openai_server_single_prompt(
        prompt, model=model, max_decode_steps=max_decode_steps,
        temperature=temperature
    )

  except openai.APIError as e:
    retry_time = getattr(e, "retry_after", None) or 30
    print(f"API error occurred. Retrying in {retry_time} seconds...")
    time.sleep(retry_time)
    return call_openai_server_single_prompt(
        prompt, model=model, max_decode_steps=max_decode_steps,
        temperature=temperature
    )

  except OSError as e:
    retry_time = 5  # Adjust the retry time as needed
    print(
        f"Connection error occurred: {e}. Retrying in {retry_time} seconds..."
    )
    time.sleep(retry_time)
    return call_openai_server_single_prompt(
        prompt, model=model, max_decode_steps=max_decode_steps,
        temperature=temperature
    )


def call_openai_server_func(
    inputs, model="gpt-3.5-turbo", max_decode_steps=20, temperature=0.8
):
  """The function to call OpenAI server with a list of input strings."""
  if isinstance(inputs, str):
    inputs = [inputs]
  outputs = []
  for input_str in inputs:
    output = call_openai_server_single_prompt(
        input_str,
        model=model,
        max_decode_steps=max_decode_steps,
        temperature=temperature,
    )
    outputs.append(output)
  return outputs


def call_palm_server_from_cloud(
    input_text, model="gemini-2.5-flash", max_decode_steps=20, temperature=0.8
):
  """Calling a Gemini model via the google-genai Cloud API.

  The legacy PaLM `text-bison` / `generateText` API has been retired by Google,
  so this routes through Gemini's `generate_content` instead. Uses the modern
  `google-genai` SDK (the `google-generativeai` package is deprecated). The
  function signature and list-of-strings return value are kept for drop-in
  compatibility with the original text-bison caller.
  """
  assert isinstance(input_text, str)
  generation_config = types.GenerateContentConfig(
      temperature=temperature,
      max_output_tokens=max_decode_steps,
  )
  max_retries = 5
  for attempt in range(max_retries):
    try:
      completion = _get_genai_client().models.generate_content(
          model=model, contents=input_text, config=generation_config
      )
      # `.text` is None (or raises) when the response carries no usable text
      # part (e.g. the output was empty or blocked); fall back to "".
      try:
        output_text = completion.text or ""
      except (ValueError, AttributeError):
        output_text = ""
      return [output_text]
    except Exception as e:  # pylint: disable=broad-except
      retry_time = 10  # Adjust the retry time as needed
      print(
          f"Gemini call error ({e}). Retrying in {retry_time} seconds "
          f"(attempt {attempt + 1}/{max_retries})..."
      )
      time.sleep(retry_time)
  raise RuntimeError(
      f"Gemini call failed after {max_retries} retries for model {model!r}."
  )
