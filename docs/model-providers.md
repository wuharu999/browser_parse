# DeepSeek, Qwen and cost planning

Checked 2026-09-10. Keep Codex for the first DeepSeek integration; do not replace the queue or add an agent framework just to change models. The current runner uses the Responses API. DeepSeek explicitly documents Codex compatibility, including `apply_patch`, tool calls and model metadata. See [DeepSeek Responses](https://api-docs.deepseek.com/guides/responses_api/) and [Codex setup](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/).

## Current implementation: DeepSeek through Codex

The example environment now selects:

```dotenv
ROBOT_CODEX_MODEL=deepseek-v4-flash-vision-exp
ROBOT_CODEX_PROVIDER_URL=https://api.deepseek.com
ROBOT_CODEX_API_KEY_ENV=DEEPSEEK_API_KEY
DEEPSEEK_API_KEY=replace-locally-never-commit
```

Keep the Cube settings from `.env.example`. Run the API and worker with `uv run --env-file .env ...` as described in the README. No host Codex fallback was added. The wrapper writes a job-local catalog for the current `deepseek-flash` and compatible legacy DeepSeek IDs, uses environment-based credentials, disables WebSockets/web search, and retains the two native subagent roles. Its catalog was matched to the installed [Codex 0.153.4 schema](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/protocol/src/openai_models.rs). Existing OpenAI/custom-provider settings still work; without explicit environment settings the worker's legacy default remains Luna.

`deepseek-v4-flash-vision-exp` is selected explicitly at the user's request. DeepSeek documents it as a retired compatibility alias served by the current vision model, `deepseek-flash`; the application does not silently rewrite the requested ID. A tiny authenticated image call using the exact requested alias returned HTTP 200 and correctly read the screenshot heading, with `deepseek-flash` in the response's model field (214 input + 6 output tokens). The authenticated model listing advertised `deepseek-flash` and `deepseek-v4-pro`. These are provider/image-call observations, not native-subagent proof. `deepseek-v4-flash` and `deepseek-v4-pro` remain text-only in our catalog. Extracting PDF text with Poppler works independently, but PDF charts/screenshots require vision or OCR. [DeepSeek vision guide](https://api-docs.deepseek.com/guides/vision/) and [Codex integration](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)

The ignored local environment has provider credentials and a healthy Cube development VM; credentials are never part of the image or repository. See [local setup](local-sandbox.md) and [current validation results](../PROGRESS.md) for the distinction between direct image tests, sandbox execution and native-subagent proof. Before public use, validate representative jobs, image/PDF handling, provider usage and whole-VM termination. No VPN software is required by the application itself; the worker must still reach its chosen provider and Cube endpoint. The previous machine checks had an active tunnel and do not establish VPN-free access.

## Qwen alternative: OpenCode

Alibaba's documented OpenAI-compatible interface uses Chat Completions. Do not point the current Codex Responses runner at a Chat Completions URL and call it compatible. OpenCode supports configurable providers and native subagents, so it is the next integration candidate if Qwen is selected. Its shared `.agents/skills/` discovery can reuse our evidence/PDF skills. Use stable `/docs/`, not the separate `/v2/docs/` configuration schema. [Alibaba text generation](https://www.alibabacloud.com/help/en/model-studio/text-generation), [OpenCode providers](https://opencode.ai/docs/providers/), [agents](https://opencode.ai/docs/agents/), [skills](https://opencode.ai/docs/skills/)

An illustrative **in-sandbox** OpenCode configuration for Beijing PAYG API access is:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "alibaba-qwen/qwen3-coder-flash",
  "provider": {
    "alibaba-qwen": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Alibaba Qwen",
      "options": {
        "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "apiKey": "{env:DASHSCOPE_API_KEY}"
      },
      "models": {"qwen3-coder-flash": {"name": "Qwen3 Coder Flash"}}
    }
  }
}
```

Use the endpoint matching the key's region; do not mix international keys with Beijing URLs. Production robot-log processing should use pay-as-you-go API credentials, not assume a personal coding subscription permits a multiuser backend. Configuration above is a documented starting point, **not installed or wired into this worker**.

OpenCode's headless command is `opencode run --format json --dir /workspace --model alibaba-qwen/qwen3-coder-flash`. A future adapter should preserve `job.json`, `activity.jsonl`, `result.json`, sandbox isolation, cancellation and the public review API. Change only the in-VM command/config/event parser; keep the public UI and Python queue. Do not start a shared privileged OpenCode server for all users. [OpenCode CLI](https://opencode.ai/docs/cli/)

## Offline cost estimator

```sh
uv run python scripts/estimate_cost.py --model deepseek-v4-flash
uv run python scripts/estimate_cost.py --model qwen3-coder-flash-cn
uv run python scripts/estimate_cost.py --model deepseek-v4-pro \
  --agents 3 --turns 10 --input-tokens 15000 --output-tokens 1500 \
  --input-growth-per-turn 1000 --cached-input-tokens 5000 \
  --minutes 10 --sandbox-hourly-usd 0.60
```

No key, network access, npm or model package is required. `--agents` includes the main agent. `--turns` is the number of billable model requests per agent; tool loops, retries and compaction add requests. Input is per request, including repeated history and cached input. Output must include reasoning tokens. Set growth to approximate expanding history; split different models or workloads into separate estimates and sum them.

Defaults mean 30 requests totaling 450,000 input and 45,000 output tokens, no caching and no infrastructure charge:

| Pricing profile | Model cost | With 1.5× planning buffer |
| --- | ---: | ---: |
| DeepSeek V4 Flash, peak | $0.2574 | $0.3861 |
| DeepSeek V4 Pro, peak | $0.7722 | $1.1583 |
| Qwen3 Coder Flash, Beijing | $0.09063 | $0.135945 |
| Qwen3 Coder Plus, Beijing | $0.36153 | $0.542295 |

These are examples under identical assumed token counts, not quality-equivalent benchmarks. DeepSeek peak rates are the conservative default; `--off-peak` halves them using the published schedule. Qwen selects the input-length tier separately for each estimated turn and deliberately assumes no cache discount. `-cn` and `-intl` are estimator profile suffixes, not API model IDs. Prices are dated snapshots from [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Alibaba Model Studio pricing](https://www.alibabacloud.com/help/en/model-studio/model-pricing); verify the account's current price/region before spending. K=1000 boundaries are conservative near binary-K cutoffs. Taxes, minimum charges and unlisted infrastructure are excluded.

OpenCode likewise tracks model input/output/reasoning/cache tokens against model price metadata; its CLI offers `stats`, and JSON events include per-step usage/cost. Never treat only the final step or parent session as the whole multi-agent bill: include child sessions and fail to “unknown” if events are incomplete. For the current worker, cost is still explicitly unknown and the API reserves USD 5 per job. The estimator does **not** automatically lower that reservation or enforce a billing cap. Do not derive costs from wall-clock minutes or zip size.
