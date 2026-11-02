# Model providers

Set `[model] provider` through `tiffin init`, `tiffin connect model` or `tiffin.toml`. The API providers use `TIFFIN_MODEL_API_KEY` or `secrets/model-api-key.txt`; a local endpoint may need no key. `auth_style = "api-key"` supports compatible endpoints that require that header instead of bearer authentication.

| Provider | Default model and key | Cost model |
| --- | --- | --- |
| `claude-cli` | `opus`; authenticate the installed Claude CLI separately | Depends on the CLI account or subscription; check its usage terms. |
| `anthropic` | `claude-sonnet-4-5`; model API key | Anthropic bills API usage by tokens. |
| `openai` | `gpt-4.1-mini`; model API key | OpenAI bills API usage by tokens. |
| `openai-compatible` | Set `model` and `base_url`; key only if your server requires one | A local model uses your own compute; a hosted compatible endpoint may charge for usage. |
| `none` | No model or key | No model charges; model-assisted work stays manual. |

Provider prices change, so check their current API pricing before enabling paid calls. A local OpenAI-compatible server such as Ollama can be used at a `/v1` endpoint; it must be running and have the chosen model installed.

```toml
[model]
provider = "openai-compatible"
model = "your-installed-model"
base_url = "http://127.0.0.1:11434/v1"
```

The optional `[model.tasks]` table overrides the model for individual tasks, such as `intent = "gpt-4.1-mini"`. Test a replacement model before relying on it: tasks expect structured responses and numbered candidate indexes. Small local models may struggle with that format. The model never supplies trusted nutrient arithmetic; Tiffin calculates it from grams and per-100 g catalogue values.

`[budgets]` limits time and turns for every task. The USD cap is enforceable only with Claude CLI; API work degrades to manual if `[models].cap_usd = true`. API providers have an operator-set `[model].api_requests_per_day` limit (default 50). It counts every HTTP attempt across the household, including retries, errors and `tiffin doctor` probes, before sending the request. The count is kept in `state/model_requests.sqlite3` across restarts and resets at midnight in the configured household time zone. **This limits request count, not spending:** token usage and provider prices vary, and a single request can cost more than another. Set a suitable count and use provider billing controls for a monetary ceiling. `tiffin doctor --offline` makes no model request.

Consent is per person and per provider. Changing provider or an OpenAI-compatible endpoint pauses model tasks until each person grants consent again with `/consent`. No consent, unavailable model or exhausted budget results in manual work and a visible job status. The optional Chrome shop reader requires Claude CLI browser tools even if another provider handles other tasks.

## Claude CLI on a server or in a container

A machine with no interactive login can authenticate the Claude CLI with a long-lived token from `claude setup-token`, set as `CLAUDE_CODE_OAUTH_TOKEN` in the process environment. Tiffin passes that one variable to the `claude` child and strips every other `CLAUDE_CODE_*` and `CLAUDECODE` variable, so a surrounding Claude Code session cannot leak into it. Keep the token in a secret mount or an environment file with restrictive permissions; it is never written to the config, the database or the logs.
