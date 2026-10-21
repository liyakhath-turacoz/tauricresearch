# Running TradingAgents: what it does, what it needs, and what you can trim

**Discovery and bulk dashboard:** see [DASHBOARD_GUIDE.md](DASHBOARD_GUIDE.md) for Global 100 / India 100 / Crypto 200 discovery, search, sectors, individual/bulk tabs, daily decision tables, saved run history and CSV export.

This guide describes the checked-out TauricResearch/TradingAgents repository. It is an LLM-driven research and decision-support framework. It does **not** connect to a broker or place real orders: the README's "simulated exchange" is an internal decision stage.

## Quick start on Windows (PowerShell)

You need Python 3.11 or newer, Git, and internet access. Python 3.13 is the version used by the repository's Docker image. If your installed version is 3.11 or 3.12, change `py -3.13` below to that version (for example, `py -3.11`).

```powershell
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
Copy-Item .env.example .env
```

Edit `.env` and set a key for one LLM provider (for example `OPENAI_API_KEY=...`). You do not need keys for every provider. Then run the interactive CLI:

```powershell
tradingagents
```

It prompts for the ticker/date, model provider and models, analysts, and report options. A first non-interactive run with the default OpenAI provider can look like this:

```powershell
$env:TRADINGAGENTS_LLM_PROVIDER = "openai"
$env:TRADINGAGENTS_QUICK_THINK_LLM = "gpt-6-luna"
$env:TRADINGAGENTS_DEEP_THINK_LLM = "gpt-6-sol"
tradingagents --ticker NVDA --date 2026-10-14 --analysts market,news --save --no-show
```

The selected model names are examples from the repository's model picker; your account must have access to them. Replace the date with a valid date no later than today. A saved run writes under `~/.tradingagents/logs` by default, including a report and, unless disabled, `complete_report.html`. Cache and the persistent decision-memory log also live under `~/.tradingagents`.

### Optional local browser launcher

The project itself has no web dashboard. This checkout now includes an optional, separate browser launcher that calls the existing CLI and shows its progress and saved HTML reports. The launcher files are outside the framework modules and can be removed without changing the TradingAgents workflow. A static HTML page alone cannot start a local command, so the launcher uses a small Python bridge bound only to `127.0.0.1`.

From PowerShell in the repository folder:

```powershell
.\Start-LocalUI.ps1
```

It opens `http://127.0.0.1:8765`. Sign in with `admin@turacoz.com` / `admin` (personal local use only). Select assets, then choose immediate analysis or the inline Schedule checkbox. All analysts are automatic; immediate runs use today in IST and schedules use the occurrence day. Starting/scheduling authorizes configured API usage. The Saved tab separates running, stopped and completed jobs, with stop/delete actions and 24-hour expiry for unfinished runs. Optional collapsed price settings offer 15/30/60-minute candles for the remaining session; no position planner or order execution is included. See README.md and DASHBOARD_GUIDE.md for the current workflow.

Stop the local launcher from its **Stop UI server** button, or run:

```powershell
.\Start-LocalUI.ps1 -Stop
```

The dashboard uses `LOCAL_UI.html`, `local_ui.css`, `local_ui.js`, `local_ui.py`, `market_discovery.py`, `analysis_jobs.py`, `ui_reports.py`, and `Start-LocalUI.ps1`. Its discovery, queue and report views are separate from the core agent workflow. No service needs to be running when you are not using the page.

Run `tradingagents --help` for flags. The package also supports `python -m cli.main` from the source checkout. Docker is available with `docker compose run --rm tradingagents`; configure `.env` first. The Docker compose setup stores run data in a named volume by default. See `docker-compose.yml` for its optional Ollama profile.

## What it does

For each ticker and analysis date, the LangGraph workflow:

1. Resolves what instrument the ticker represents and fetches market/company context.
2. Runs the selected analysts concurrently. The available analysts are market/technical, sentiment, news/macro, and fundamentals.
3. Has bullish and bearish researchers debate the analyst evidence; a research manager synthesizes it.
4. Has a trader propose a direction/position and sizing. Three risk personas challenge that proposal, and a portfolio manager makes the final simulated decision.
5. Writes a decision/report, can render a single-page HTML report, and appends the decision to a persistent memory log. Later runs can settle matured past decisions and give lessons to the portfolio manager.

It can also run a historical backtest over ticker/date grids and score decisions against realized returns and regional benchmarks. A historical date does not make every input historical: the code documents that social feeds are recent/unarchived, and Yahoo statements for non-US companies cannot be reliably dated by filing time. Treat backtest outcomes as research results, not proof of future performance.

## What external services and keys are needed

You need internet access. At minimum, choose **one** LLM backend and provide that backend's credentials (unless running a local model). Data sources are separate from the LLM provider.

| Service | Used for | Key needed? | Default / optional |
|---|---|---:|---|
| Chosen LLM API | Analyst, researcher, trader, debate, and manager calls | Yes for hosted providers; no for local Ollama | Required |
| Yahoo Finance via `yfinance` | Prices, technical inputs, company overview, news and many financial statements; also ticker identity | No | Default for prices, indicators, and news; fallback for fundamentals |
| SEC EDGAR | US company statements as filed, for point-in-time fundamentals | No; set `SEC_EDGAR_USER_AGENT` to a contact name/email | Default first choice for US statements |
| StockTwits and Reddit public feeds | Sentiment analyst's social inputs | No app key in this code path | Optional analyst enrichment; feeds may rate-limit or be unavailable |
| FRED | Macro time series such as rates, CPI, labor and growth | `FRED_API_KEY` (free) | Optional enrichment; default macro vendor, but the news analyst can continue if unavailable |
| Polymarket public Gamma API | Prediction-market context for the news analyst | No | Optional enrichment |
| Alpha Vantage | Alternative prices, indicators, fundamentals and news | `ALPHA_VANTAGE_API_KEY` | Optional; configure as a vendor or fallback |
| TypeSafe Jev | Screens StockTwits/Reddit posts for relevance and labels stance | `TYPESAFE_API_KEY` | Optional; without it social posts are not screened |
| Tauric announcements endpoint | CLI startup announcements | No user key; short timeout/fallback | Cosmetic; not needed for analysis |

Default vendor choices are in `tradingagents/default_config.py`: Yahoo for prices/indicators/news, SEC EDGAR then Yahoo for statements, FRED for macro and Polymarket for prediction markets. A missing optional enrichment does not normally prevent a run. To use Alpha Vantage, configure `data_vendors` in Python or change the relevant category in `DEFAULT_CONFIG`; the code does not automatically try vendors you did not configure.

## AI / LLM providers supported

The provider name is the value used for `llm_provider` (or per-tier provider). Keys and settings below match `tradingagents/llm_clients/api_key_env.py` and the provider clients. Hosted inference is billed/rate-limited by its provider; the project does not bundle model weights.

| Provider | Key / setup | Notes |
|---|---|---|
| OpenAI (`openai`) | `OPENAI_API_KEY` | GPT models; default provider in the config. |
| Anthropic (`anthropic`) | `ANTHROPIC_API_KEY` | Claude models; native Anthropic client. |
| Google (`google`) | `GOOGLE_API_KEY` | Gemini models; supports thinking-level configuration. |
| Azure OpenAI (`azure`) | `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`; optional `OPENAI_API_VERSION` | You supply a deployment name and Azure resource. |
| Amazon Bedrock (`bedrock`) | AWS credential chain / `AWS_PROFILE`, or `AWS_BEARER_TOKEN_BEDROCK`; set region | Optional install: `python -m pip install ".[bedrock]"`. Uses Bedrock model or inference-profile IDs. |
| xAI (`xai`) | `XAI_API_KEY` | Grok through an OpenAI-compatible API. |
| DeepSeek (`deepseek`) | `DEEPSEEK_API_KEY` | DeepSeek models; client handles provider-specific reasoning round trips. |
| Qwen international (`qwen`) | `DASHSCOPE_API_KEY` | Alibaba DashScope international endpoint. |
| Qwen China (`qwen-cn`) | `DASHSCOPE_CN_API_KEY` | Separate China endpoint/account key. |
| GLM international (`glm`) | `ZHIPU_API_KEY` | Z.AI endpoint. |
| GLM China (`glm-cn`) | `ZHIPU_CN_API_KEY` | BigModel endpoint. |
| MiniMax global (`minimax`) | `MINIMAX_API_KEY` | Global endpoint. |
| MiniMax China (`minimax-cn`) | `MINIMAX_CN_API_KEY` | China endpoint. |
| Kimi / Moonshot (`kimi`) | `MOONSHOT_API_KEY` | Moonshot API. |
| OpenRouter (`openrouter`) | `OPENROUTER_API_KEY` | Choose a model available to your OpenRouter account. |
| Mistral (`mistral`) | `MISTRAL_API_KEY` | Mistral API. |
| Groq (`groq`) | `GROQ_API_KEY` | Choose a model served by Groq. |
| NVIDIA NIM (`nvidia`) | `NVIDIA_API_KEY` | Choose a model available through NVIDIA NIM. |
| Ollama (`ollama`) | No key by default; optional `OLLAMA_BASE_URL` | Local or remote Ollama server; download model weights separately with `ollama pull <model>`. |
| Generic OpenAI-compatible (`openai_compatible`) | Usually no key locally; optional `OPENAI_COMPATIBLE_API_KEY` | vLLM, LM Studio, llama.cpp server, or another compatible endpoint. Set `backend_url` / `TRADINGAGENTS_LLM_BACKEND_URL`. |

The quick tier handles analysts, researchers, debaters and trader; the deep tier handles research and portfolio managers. They can share one provider or use different providers/keys. Model menus in `tradingagents/llm_clients/model_catalog.py` are curated suggestions, not an exhaustive list: several providers accept a custom model ID. Feature support can vary by model (for example structured output and tool calling).

## RAM and hardware estimate

The project itself does not publish a measured minimum RAM benchmark. These are practical planning estimates, not guarantees:

| Setup | Suggested host memory |
|---|---:|
| Hosted LLM APIs (OpenAI, Anthropic, etc.), one ticker at a time | 4 GB can work; 8 GB is a comfortable starting point |
| Backtests / several runs at once | 8–16 GB or more, depending on parallelism and retained data |
| Local Ollama, 8B quantized model | Plan for about 8 GB total system RAM at minimum; 16 GB is more comfortable |
| Local Ollama, 20B quantized model | Roughly 16 GB or more; 24 GB is safer |
| Local Ollama, 30B quantized model | Roughly 24 GB or more; GPU VRAM may be needed for usable speed |

With hosted LLMs, model weights stay on the provider's servers, so this is mainly Python, LangGraph, pandas/data, caches and report generation. Local model requirements depend on quantization, context length, CPU/GPU offload and concurrent requests. Weight storage alone is approximately parameter count × bits per parameter ÷ 8; runtime memory is higher because of the model runtime, context/KV cache and the operating system. A 4 GB Docker memory limit is therefore a reasonable first try for a cloud-backed single run, but not a benchmarked guarantee. For local models, check the particular model's Ollama page and your GPU's available VRAM before downloading.

## What can be removed or disabled

These are reasonable choices to make before editing code:

| If you want... | You can... | Trade-off |
|---|---|---|
| Fewer LLM calls / lower run cost | Select fewer analysts (`market,news`, or just `market`); set debate and risk rounds to 1; lower `max_tool_rounds` if desired | Less evidence and/or less iteration. Research, trader, risk, and portfolio-manager stages remain part of the graph. |
| No sentiment/social inputs | Omit the `sentiment` analyst (its internal key is `social`) | No StockTwits/Reddit narrative. No TypeSafe key is needed if you do not want screening. |
| No macro/prediction context | In a fork, remove or guard the FRED and Polymarket tools in the news analyst | These are enrichment, not core price data. The current vendor router has no supported `none` vendor value. |
| Fewer paid data vendors | Keep Yahoo defaults; do not configure Alpha Vantage or TypeSafe | Yahoo is keyless but is an unofficial, rate-limited data path and availability can change. |
| No persistent decision memory | For a fork, remove/disable the memory node and settlement path; in normal use redirect `TRADINGAGENTS_MEMORY_LOG_PATH` to an isolated file | The memory step is always wired into the current graph; path override isolates data but does not turn the feature off. |
| No saved HTML | Run with `--no-html` | Keeps the text report, skips the single-page HTML output. |
| No Docker/local inference | Use Python + hosted LLM provider | Avoids managing model weights; ongoing API charges and network dependency remain. |

The lowest-effort setup is one hosted model provider, default Yahoo data, and only `market` plus `news` analysts. `FRED_API_KEY`, Alpha Vantage, TypeSafe, AWS Bedrock extras, and local Ollama weights are not required for that setup. If you want to eliminate more than optional analysts/data feeds, expect code changes: the debate, trader, risk and portfolio manager are structural parts of the workflow.

## Useful configuration pointers

- `.env.example`: API keys and common CLI overrides. Keep real keys out of source control.
- `tradingagents/default_config.py`: defaults for providers, model tiers, vendors, report paths, and debate limits.
- `tradingagents/dataflows/router.py`: available data vendors and tool-to-vendor routing.
- `tradingagents/llm_clients/api_key_env.py`: provider-to-key mapping.
- `tradingagents/llm_clients/model_catalog.py`: model picker suggestions.
- `tradingagents/graph/setup.py`: analyst and decision workflow wiring.
- `README.md`: Python API, portfolio input, persistence, checkpoint resume and backtesting examples.

## Source of this guide

Repository: <https://github.com/TauricResearch/TradingAgents>. This guide summarizes the checked-out README and source files; provider offerings, model IDs, and third-party API behavior can change independently of this repository.
