# 常用指令的捷徑；實際邏輯都在 build.py（沒有 make 時直接用 python build.py 也一樣）
#   make test                       跑全部自動化檢查
#   make build                      打包資料夾版並產生發行用 zip
#   make onefile                    打包成單一 exe
#   make release                    發行下一個修訂版（1.0.0 已發行 → 發行 1.0.1；檢查 → 打包 → 打 tag 推上去 → 上傳 Release）
#   make release VERSION=1.2.0      指定版本：先把版本號改成 1.2.0 並 commit，再發行
#   make release-dry VERSION=1.2.0  演練發行：只檢查與打包，不 commit、不打 tag、不上傳
#   make publish TAG=v1.2.0         只把已打包的 zip 上傳到 GitHub Release
#   make icon                       重新產生程式圖示
#   make clean                      刪除打包產生的 build、dist

PYTHON ?= python
VERSION_ARG = $(if $(VERSION),--version $(VERSION),)

.PHONY: help test build onefile release release-dry publish icon clean

help:
	$(PYTHON) build.py --help

test:
	$(PYTHON) -m unittest discover -s tests -t .

build:
	$(PYTHON) build.py

onefile:
	$(PYTHON) build.py --onefile

release:
	$(PYTHON) build.py release $(VERSION_ARG)

release-dry:
	$(PYTHON) build.py release --dry-run $(VERSION_ARG)

publish:
	$(PYTHON) build.py publish --tag $(TAG)

icon:
	$(PYTHON) tools/make_icon.py

clean:
	$(PYTHON) -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('build', 'dist')]"
