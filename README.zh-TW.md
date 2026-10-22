# zh-community-radar

[English](README.md)

Radar 會定時去看幾個公開的科技社群，把新文章和挑出來的留言整理成一份短短的摘要，每一條都附原文連結。除了 Hacker News、GitHub Release、個人部落格 RSS、Lobsters、Hugging Face 這些英文來源，它也能追 **PTT** 和 **V2EX**。市面上英文的同類工具不少，但會顧到華語社群的不多，這是我做它的主要原因。

摘要裡的每一條都能點回原文，會記下抓到的時間；某個來源這次沒抓完整，也會直接寫出來。Radar 只整理它實際抓到的內容，不會說成「整個社群都這樣想」。

> 目前狀態：alpha（`0.3.0.dev0`）。抓取、事件整理、摘要和查詢都能用，也有測試。還沒有架好的服務、沒有內建的寄送功能，也還沒有 MCP server。

## 可以做什麼

- **有節制地抓資料**：每個來源有自己的間隔，每次執行有請求數和時間上限。網站回 `Retry-After` 會記下來並照著等，只連白名單上的網域。
- **保留每個版本**：用 SQLite 存每篇文章、每則留言的每一次修改，重跑不會重複寫入，內容被改過也看得出來。
- **整理成事件**：同一件事的多篇文章和留言會合成一個事件，保留引文來源。
- **可選的 AI 分析**：每個事件呼叫一次模型，模型不能用任何工具。有每次、每天、每月的額度上限，也會自動檢查引文是不是真的出現在原文裡。支援 Claude Code CLI，或任何 OpenAI 相容的 `/chat/completions` 端點。
- **摘要和提問**：把檢查過的分析整理成 Markdown／JSON 摘要；也可以直接對本機資料提問，不會另外上網搜尋。
- **方便給 AI agent 用的 CLI**：每個指令都只輸出一個 JSON，狀態和結束碼固定，任何 agent 或腳本都能直接呼叫。
- **備份**：SQLite 一致性快照，附雜湊和資料表檢查；還原時只會寫到新資料夾，並以唯讀的 shadow 模式開啟。

## 支援的來源

| Adapter | 來源 | 說明 |
|---|---|---|
| `hn` | Hacker News（Firebase API） | 最新和熱門文章，抽幾串留言 |
| `feed` | 任何 RSS／Atom | GitHub 的 `releases.atom`、`tags.atom`、部落格、電子報 |
| `ptt` | PTT 網頁版看板（例如 Soft_Job） | **預設關閉**，請先看下面的使用原則 |
| `v2ex` | V2EX 公開 API v1 | 熱門主題和部分回覆 |
| `lobsters` | Lobsters JSON | 文章和留言 |
| `hf` | Hugging Face 模型討論區 | 指定 repo 的近期討論 |
| `reddit_rss` | Reddit 看板 RSS | **預設關閉**，匿名 RSS 常被限流 |

想加新來源，寫一個 adapter 函式就好，做法在 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 快速開始

需要 Python 3.11 以上。

```bash
git clone https://github.com/boowenchen/zh-community-radar.git
cd zh-community-radar
python -m venv .venv
source .venv/bin/activate          # Windows：.venv\Scripts\activate
pip install -e ".[dev]"

cp config/sources.example.json config/sources.local.json
radar capabilities
radar collect --due --max-requests 50 --max-seconds 120
radar events rebuild
radar events list --limit 5
radar ask "最近大家怎麼看 llama.cpp？"
```

`config/*.local.json`、`var/` 和資料庫檔都已經寫進 `.gitignore`。資料預設放在 `var/radar`，可以用 `--data-dir` 或環境變數 `RADAR_DATA_DIR` 改位置。

### 想加上 AI 分析的話

```bash
# 用 Claude Code CLI（走你自己的訂閱，呼叫時會關掉工具、MCP 和外掛）
cp config/model.claude_cli.example.json config/model.local.json

# 或任何 OpenAI 相容端點（LiteLLM、vLLM、Ollama、雲端 API 都可以）
cp config/model.openai_compatible.example.json config/model.local.json
export RADAR_LLM_API_KEY=...       # 變數名稱在 http.api_key_env 設定

radar analyze --limit 4
radar digest build --limit 8
radar usage --scope day
```

只有固定的本機或內網 IP，而且設定了 `"allow_private_http": true`，才能用 `http://`，其他一律要 HTTPS。API key 只在呼叫當下從環境變數讀取，不會寫進設定檔。

## CLI

共同參數要放在指令**前面**：`radar [--data-dir 資料夾] [--config 檔案] [--ledger-dir 資料夾] [--run-id ID] [--idempotency-key KEY] <指令>`。

| 指令 | 用途 |
|---|---|
| `capabilities` | 版本、可用指令、adapter 清單 |
| `collect --due --sources a,b --max-requests N --max-seconds S` | 抓到期的來源，寫入文章、版本和進度，不會呼叫模型 |
| `sources status`、`items --limit N` | 來源狀態、原始資料 |
| `changes --after CURSOR` | 變動清單，附 `next_cursor` |
| `events rebuild`、`events list`、`events show --event-id ID` | 建立和查詢事件 |
| `analyze --model-config 檔案 --limit N` | 有額度限制的 AI 分析，會檢查引文 |
| `digest build --since ISO --cutoff ISO --limit N` | 產生不會再變動的 Markdown／JSON 摘要 |
| `ask "問題"` | 對本機資料提問，附證據和資料缺口 |
| `feedback add --target-id ID --label useful` | 記錄讀者回饋 |
| `usage --scope run\|day\|month` | 模型用量，分開列實際用量和預留量 |
| `outbox` | 寄送佇列狀態 |
| `backup 資料夾`、`verify-snapshot 資料夾`、`restore 快照 新資料夾` | 備份和還原 |

輸出固定是一個 JSON，`status` 會是 `success`、`partial`、`failed` 或 `skipped`，結束碼分別是 0、2、1、3。請先看 `status` 和 `errors`，再讀 `data`。

`python -m radar.runtime --runtime-config 檔案` 會跑一輪有上限的流程（抓取 → 整理事件 → 讀變動 → 視設定分析和產生摘要），適合放進 cron 或 systemd timer。範本在 `deploy/runtime.example.json`，裡面標了 `proposal_only`，要先複製一份改好才能用。

## 設定

- `config/sources.example.json`：來源清單。每個來源有 `id`、`adapter`、`enabled`、`interval_minutes` 和各自的選項。最外層可以加 `"user_agent"`（或設 `RADAR_USER_AGENT`）換掉預設的 `Radar/<版本> (+https://github.com/boowenchen/zh-community-radar)`。
- `config/model.*.example.json`：模型、額度和各種上限。
- `.env.example`：會用到的環境變數名稱。

## 使用原則

Radar 只抓**公開**的網頁和 API。不登入、不帶 cookie、不用私人 token，User-Agent 也會附上這個專案的網址，讓站方知道是誰在抓。

- 頻率放慢。範例設定每個來源 30 到 180 分鐘才抓一次，頁數和留言數也壓得很低。不是你自己的網站，請不要再調快。
- 遵守各站的 `robots.txt`、流量限制和使用條款。遇到 429 或 403，Radar 會照 `Retry-After` 等待，不會硬重試。
- 範例設定裡 PTT 和 Reddit 是關閉的，要打開之前請先看過這兩個站目前的規定。
- 引用短短一段就好，附上原文連結。不要整篇轉貼，也不要拿來做鏡像站。
- 摘要描述的是這次抓到的樣本，不代表社群整體意見。

## 開發

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
pip-audit
```

`requirements-runtime.lock` 和 `requirements-dev.lock` 鎖定測過的版本。`pywin32` 只會在 Windows 上安裝（`portalocker` 需要）。

## 授權

[MIT](LICENSE)
