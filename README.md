# MTool 翻譯器

把 MTool 匯出的原文檔翻譯成可以直接載回 MTool 的翻譯檔。支援雲端 API、免費服務、本地 AI、本地 LLM，並用術語表與前文參考維持名詞一致。

## 安裝與啟動

### 直接用打包好的程式（不需要 Python）

到 GitHub 的 [Releases](https://github.com/deadline314/mtool-translate/releases) 下載最新的 `MToolTranslator-v<版本>-windows.zip`，解壓後打開 `MTool翻譯器\MTool翻譯器.exe`。可以對它按右鍵 →「傳送到」→「桌面（建立捷徑）」，之後從桌面一鍵開啟。

設定與記錄存在 exe 旁邊，整個資料夾帶著走就好；更新版本時把新的 zip 解壓覆蓋舊資料夾，設定會保留。

### 自己打包

```bash
make build
```

沒有 make 的話用 `python build.py`，效果一樣。完成後程式在 `dist\MTool翻譯器\MTool翻譯器.exe`，發行用的 zip 在 `dist\MToolTranslator-v<版本>-windows.zip`。

打包腳本會依序：安裝打包工具 → 跑全部自動化檢查 → 打包 → 用打包好的程式跑自我檢查（包含真的把主視窗建起來），任何一步失敗就停止。所有打包設定都在 `build.py` 裡，不需要另外維護設定檔。

| 打包方式 | 指令 | 啟動時間（實測） | 適合 |
|---|---|---|---|
| 資料夾版（預設） | `make build` | 約 0.6 秒 | 日常使用 |
| 單一 exe | `make onefile` | 約 1.7 秒 | 只想傳一個檔案 |

單一 exe 每次開啟都要先把內容解壓到暫存資料夾，所以比較慢，也比較容易被防毒軟體誤判。

### 發行新版本

```bash
make release
```

不指定版本時，會發行下一個修訂版：目前的 1.0.0 已經發行過，就自動改成 1.0.1 再發行。要跳到其他版本就指定，例如 `make release VERSION=1.2.0`（不能比目前的版本舊）。

會依序：檢查 git 狀態（要在 main、沒有未 commit 的修改、沒有落後遠端、版本沒發行過）→ 把版本號改好並 commit → 跑全部檢查並打包 → 推 commit、打 tag（例如 `v1.0.1`）並推上去 → 把 zip 上傳到 GitHub Release。

- 打包失敗時，改好的版本號只會留在本機、不會推上去；修正後再執行一次 `make release`，會接著發行同一個版本，不會跳號。
- 推 tag 後 GitHub Actions 也會自動打包並上傳；本機與 GitHub Actions 走同一套步驟，誰先完成都沒關係，Release 已存在時會覆蓋同名附件。所以 GitHub Actions 不能用時，本機照樣能完整發行；本機沒裝 gh 時，則交給 GitHub Actions 上傳。
- 想先演練：`make release-dry`（也可以加 `VERSION=1.2.0`），只做檢查與打包，不 commit、不打 tag、不上傳。
- 只想補傳附件：`make publish TAG=v1.2.0`。
- 上傳 Release 需要 [GitHub CLI](https://cli.github.com/) 並登入（`gh auth login`）。
- 每次推到 main 或開 PR，GitHub Actions 會在 Windows 上跑全部自動化檢查；在 Actions 頁面手動執行「Release」則只打包並把 zip 存成 Artifact，不會發行。

### 從原始碼執行

需要 Python 3.10 以上（Windows 版 Python 已內建 tkinter）。

```bash
pip install -r requirements.txt   # 只有用 Claude 才需要
python app.py
```

第一次開啟會顯示使用說明；之後可以按右下角的「使用說明」再看。把滑鼠停在任何欄位或按鈕上都有說明，視窗左下角會提示下一步該做什麼。

## 使用流程

1. 用 MTool 開啟遊戲 → 翻譯頁 →「導出原文」，得到 `ManualTransFile.json`。
2. 把這個檔案拖進翻譯器視窗（或按「瀏覽…」選擇，或直接拖到 exe 圖示上開啟），譯文預設輸出到同資料夾的 `ManualTransFile_translated.json`。
3. 選翻譯引擎，按「測試連線」確認可用。
4. （建議）按「自動擷取術語」，檢查、修改術語表裡的譯名。
5. 按「開始翻譯」。
6. 回到 MTool 翻譯頁 →「加載翻譯文件」，選擇輸出的檔案。

翻譯中途停止、程式關閉或網路中斷都沒關係：每隔幾秒會自動存檔，再按一次「開始翻譯」會從中斷處接著翻，已翻好的不會重送。輸出檔被其他程式鎖住時，結果會另存成 `*.recovered.json`，不會遺失。

## 翻譯引擎

| 類別 | 服務 | 說明 |
|---|---|---|
| 雲端 API | Claude、OpenAI、Gemini、DeepSeek、任何 OpenAI 相容服務 | 品質最好，支援上下文與術語表 |
| 免費服務 | MyMemory、Google 翻譯（非官方） | 不需金鑰；沒有上下文，術語用替換方式強制套用 |
| 本地 AI | Sakura（llama.cpp server） | 日翻中專用模型，使用 Sakura 自己的提示格式，輸出簡體中文 |
| 本地 LLM | Ollama、LM Studio | 完全離線，支援上下文與術語表 |

- 預設模型名稱可能過時，可按「取得模型清單」從服務端抓。
- MyMemory 匿名每天約 5,000 字，填 Email 可提高到約 50,000 字。
- Ollama 走原生 API 並固定上下文長度 8192，避免預設長度默默截掉提示詞。
- 本地模型建議「同時請求數」設 1、「每批條數」10 左右。

## 名詞一致性

- **術語表**：每批只把該批（含前文）出現的術語送進提示詞，要求使用指定譯名；翻完會檢查，沒用到指定譯名的條目會自動重翻一次。
- **自動擷取術語**：先讓 LLM 掃過全文，列出角色名、地名、物品、技能等專有名詞並給譯名；模型編造、原文沒出現的詞會被濾掉。
- **前文參考**：每批附上前面幾句的原文與已完成的譯文。批次是邊做邊送，後面的批次看得到前面剛翻好的譯文。
- **遊戲背景**：可以寫角色設定、語氣，會放進系統提示詞。

術語表存在原文檔旁邊的 `*.glossary.json`，格式與 MTool 翻譯檔相同（`{"原文": "譯名"}`）。把別的 `*.glossary.json` 拖進視窗會合併進目前的術語表。

## 控制碼保護

送出翻譯前，控制碼會換成 `[[0]]` 這類佔位符，翻完再還原：RPG Maker 的 `\C[2]`、`\V[1]`、`\N[1]`、`\{`，Unity／YEP 的 `<color=red>`、`<WordWrap>`，Ren'Py 的 `{w=0.5}`、`[name]`，Tyrano／KiriKiri 的 `[r]`，以及 `%1`、`%02d`、`{0}` 等格式字串。Wolf RPG 的注音 `\r[漢字,かんじ]` 只保留本文。`<勇者の剣>` 這種角括號裡是日文的會當成文字翻譯。

佔位符少了或多了的條目會重試一次，仍失敗就保留原文，避免壞掉的控制碼讓遊戲出錯。

## 錯誤處理

| 狀況 | 處理方式 |
|---|---|
| 網路斷線、伺服器忙（5xx）、限流（429） | 等待後重試（遵守 Retry-After），等待中按停止會立刻結束 |
| 請求太長、被安全機制拒絕、輸出被截斷 | 把批次對半拆開重送，只有真正有問題的那句保留原文 |
| 金鑰錯誤、模型不存在、額度用完 | 立即中止並說明原因，已完成的部分都有存檔 |
| 連續多批全部失敗、或連續多次請求被拒絕 | 判定設定有問題，自動中止，避免白白浪費額度 |
| 既有輸出檔損壞 | 拒絕覆蓋，請使用者先改名或移走 |

詳細記錄寫在程式旁邊的 `logs/mtool-translate.log`（自動輪替，最多約 4 MB）。程式放在不能寫入的資料夾（例如 Program Files）時，設定與記錄改存到 `%APPDATA%\MToolTranslator`。

遇到問題時可以執行 `MTool翻譯器.exe --self-check`（或 `python app.py --self-check`），會在旁邊產生 `self-check.txt`，列出每個元件是否正常。

## 開發

```bash
make test
```

（或 `python -m unittest discover -s tests -t .`）

- `translator/`：翻譯核心，不依賴介面
  - `engines.py`：引擎與服務清單。新增引擎：寫一個子類別、在 `_FACTORIES` 註冊、在 `PROVIDERS` 加一筆，介面會自動出現
  - `pipeline.py`：批次、執行緒池、重試、斷路器、存檔、續翻、術語擷取
  - `formats.py`：翻譯檔格式。新增格式（例如 Unity XUnity）：繼承 `TranslationFormat`
  - `text.py`：控制碼遮罩、回應解析、術語表整理
  - `errors.py`：錯誤分類
- `app.py`：Tkinter 介面；`ui_widgets.py`：Hover 說明、使用說明、檔案拖放；`settings.py`：設定讀寫與舊版轉換；`runtime.py`：設定存放位置、打包資源、自我檢查
- `build.py`：打包與發行；`Makefile`：常用指令捷徑；`.github/workflows/`：自動檢查與自動發行；`tools/make_icon.py`：重新產生圖示（只有改圖示時需要）
- 版本號只寫在 `translator/__init__.py` 的 `__version__`，視窗標題、自我檢查、發行檔名都從這裡讀
- `tests/`：自我檢查。引擎測試用本機假伺服器模擬各家 API，介面測試會開真的視窗操作一遍
- `settings.json`：介面設定與 API 金鑰（明碼存放在本機，已加入 `.gitignore`）
