"""
打包與發行。所有設定都在這支腳本裡（不需要維護 .spec 檔）；Makefile 與 GitHub Actions 都只是呼叫它。

    python build.py                       打包資料夾版（啟動快）並產生發行用 zip
    python build.py --onefile             打包成單一 exe（好傳送，但每次啟動要先解壓，比較慢）
    python build.py release               本機完整發行；目前版本已發行過就自動把修訂版號 +1（1.0.0 → 1.0.1）
    python build.py release --version 1.2.0
                                          指定版本：先把版本號改成 1.2.0 並 commit，再發行
    python build.py release --dry-run     只做檢查與打包，不 commit、不打 tag、不上傳
    python build.py publish --tag v1.2.0  把已打包的 zip 上傳到 GitHub Release（CI 用）

發行步驟：檢查 git 狀態 → （改版本號並 commit）→ 跑全部自動化檢查 → 打包 → 用打包好的程式跑自我檢查
→ 推 commit、打 tag 並推上去 → 上傳 zip 到 GitHub Release。任何一步失敗就停止。
推 tag 也會觸發 GitHub Actions 做同樣的事；兩邊誰先完成都沒關係，Release 已存在時會覆蓋同名附件。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "MTool翻譯器"
ASSET_PREFIX = "MToolTranslator"  # Release 附件用英文檔名，避免 GitHub 改掉中文檔名
ICON = os.path.join(ROOT, "assets", "app.ico")
VERSION_FILE = os.path.join(ROOT, "translator", "__init__.py")
VERSION_RE = re.compile(r'^__version__ = "([^"]+)"', re.M)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
DIST, WORK = os.path.join(ROOT, "dist"), os.path.join(ROOT, "build")
# 程式用不到、但可能被其他套件順帶拉進來的大型模組，排除可縮小體積、加快啟動
EXCLUDES = ["tests", "tools", "unittest", "pydoc", "doctest", "PIL", "numpy", "pytest", "IPython"]


class BuildError(Exception):
    pass


def run(cmd, capture=False, check=True):
    print("$", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                       capture_output=capture)
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip() if capture else ""
        raise BuildError(f"指令失敗（{r.returncode}）：{' '.join(cmd)}" + (f"\n{detail}" if detail else ""))
    return (r.stdout or "").strip() if capture else r.returncode


# ---------------------------------------------------------------- 版本號

def read_version():
    with open(VERSION_FILE, encoding="utf-8") as f:
        m = VERSION_RE.search(f.read())
    if not m:
        raise BuildError(f"找不到版本號：{VERSION_FILE}")
    return m.group(1)


def write_version(version):
    with open(VERSION_FILE, encoding="utf-8", newline="") as f:  # 保留原本的換行字元
        text = f.read()
    with open(VERSION_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(VERSION_RE.sub(f'__version__ = "{version}"', text, count=1))


def asset_path(version):
    return os.path.join(DIST, f"{ASSET_PREFIX}-v{version}-windows.zip")


# ---------------------------------------------------------------- 打包

def _remove(path):
    try:
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    except PermissionError as e:
        raise BuildError(f"無法刪除舊的打包結果（{path}），請先關閉正在執行的翻譯器再打包：{e}")


def build(onefile=False, skip_tests=False):
    """打包並驗證。資料夾版另外產生發行用 zip。回傳 zip 路徑（單一 exe 版回傳 exe 路徑）。"""
    version = read_version()
    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "pyinstaller", "-r", "requirements.txt"])
    if not skip_tests:
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."])

    target = os.path.join(DIST, NAME + (".exe" if onefile else ""))
    if os.path.exists(target):
        _remove(target)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--onefile" if onefile else "--onedir",
           "--name", NAME, "--icon", ICON, "--add-data", f"{ICON}{os.pathsep}assets",
           "--distpath", DIST, "--workpath", WORK, "--specpath", WORK]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    run(cmd + [os.path.join(ROOT, "app.py")])

    exe = target if onefile else os.path.join(target, NAME + ".exe")
    code = run([exe, "--self-check"], check=False)
    report = os.path.join(os.path.dirname(exe), "self-check.txt")
    with open(report, encoding="utf-8") as f:
        print(f.read())
    os.remove(report)
    if code != 0:
        raise BuildError("打包後的自我檢查沒有通過，請看上面的報告")

    if onefile:
        print(f"完成：{exe}（{os.path.getsize(exe) / 1e6:.1f} MB）")
        return exe
    for old in os.listdir(DIST):
        if old.startswith(ASSET_PREFIX) and old.endswith(".zip"):
            _remove(os.path.join(DIST, old))
    zip_path = shutil.make_archive(asset_path(version)[:-4], "zip", DIST, NAME)
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(target) for f in fs)
    print(f"完成：{exe}（{size / 1e6:.1f} MB）")
    print(f"發行用 zip：{zip_path}（{os.path.getsize(zip_path) / 1e6:.1f} MB）")
    return zip_path


# ---------------------------------------------------------------- 發行

def gh_ready():
    """有安裝 gh 並且已登入才能上傳 Release。"""
    if not shutil.which("gh"):
        return False
    return subprocess.run(["gh", "auth", "status"], cwd=ROOT, capture_output=True).returncode == 0


def publish(tag):
    """上傳 zip 到 GitHub Release。Release 已存在（例如另一邊先完成）就覆蓋同名附件。"""
    version = read_version()
    if tag != f"v{version}":
        raise BuildError(f"tag {tag} 跟程式版本 {version} 對不上；請先把版本號改成 {tag.lstrip('v')}")
    zip_path = asset_path(version)
    if not os.path.exists(zip_path):
        raise BuildError(f"找不到要上傳的 zip：{zip_path}，請先執行 python build.py")
    if not gh_ready():
        raise BuildError("找不到 gh 或尚未登入（gh auth login），無法上傳 Release")
    exists = subprocess.run(["gh", "release", "view", tag], cwd=ROOT, capture_output=True).returncode == 0
    if not exists:
        created = run(["gh", "release", "create", tag, zip_path, "--title", f"{NAME} {tag}",
                       "--generate-notes", "--verify-tag"], check=False)
        if created == 0:
            print(f"已建立 Release {tag}")
            return
        print("建立 Release 失敗，可能另一邊剛建立好，改用上傳附件")
    run(["gh", "release", "upload", tag, zip_path, "--clobber"])
    print(f"已上傳附件到 Release {tag}")


def _git(*args):
    return run(["git", *args], capture=True)


def _semver(version):
    if not SEMVER_RE.match(version or ""):
        raise BuildError(f"版本號格式要像 1.2.0：{version}")
    return tuple(int(x) for x in version.split("."))


def bump_patch(version):
    major, minor, patch = _semver(version)
    return f"{major}.{minor}.{patch + 1}"


def _tag_exists(tag):
    return bool(_git("tag", "--list", tag) or _git("ls-remote", "--tags", "origin", f"refs/tags/{tag}"))


def choose_version(current, requested):
    """
    要發行哪個版本：有指定就用指定的；沒指定時，目前版本已經發行過就把修訂版號 +1（1.0.0 → 1.0.1），
    還沒發行過就發行目前版本（例如上次打包失敗、版本號已改好但還沒推上去，重跑時接著發同一版，不會跳號）。
    """
    if requested:
        if _semver(requested) < _semver(current):
            raise BuildError(f"指定的版本 {requested} 比目前的 {current} 還舊")
        return requested
    return bump_patch(current) if _tag_exists(f"v{current}") else current


def release(new_version=None, dry_run=False, skip_tests=False):
    current = read_version()
    _semver(current)
    if new_version:
        _semver(new_version)

    # 1. git 狀態：在 main、沒有未 commit 的修改、沒有落後遠端
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise BuildError(f"請在 main 分支發行（目前在 {branch}）")
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise BuildError("有尚未 commit 的修改，請先 commit 或還原後再發行")
    _git("fetch", "origin", "--tags")
    if int(_git("rev-list", "--count", "HEAD..origin/main") or 0):
        raise BuildError("本機 main 落後 origin/main，請先 git pull")
    version = choose_version(current, new_version)
    tag = f"v{version}"
    if _tag_exists(tag):
        raise BuildError(f"版本 {version} 已經發行過，請指定新的版本號，例如 make release VERSION={bump_patch(version)}")
    if not new_version and version != current:
        print(f"版本 {current} 已經發行過，這次自動發行 {version}")
    can_publish = gh_ready()
    if not can_publish:
        print("提醒：找不到 gh 或尚未登入，等一下只會推 tag，由 GitHub Actions 上傳 Release；"
              "也可以之後安裝 gh、登入後執行 python build.py publish --tag " + tag)

    if dry_run:
        build(skip_tests=skip_tests)
        print(f"\n[演練] 檢查與打包都通過。正式執行時會："
              f"{f'把版本號從 {current} 改成 {version} 並 commit、' if version != current else ''}"
              f"推 main、建立並推送 tag {tag}、"
              f"{'上傳 zip 到 GitHub Release' if can_publish else '交給 GitHub Actions 上傳 Release'}。")
        return

    # 2. 改版本號並 commit（重跑時版本號已經一樣就跳過，不會重複 commit）
    if version != current:
        write_version(version)
        run(["git", "add", VERSION_FILE])
        run(["git", "commit", "-m", f"Release {tag}"])

    # 3. 打包與驗證
    try:
        build(skip_tests=skip_tests)
    except BuildError:
        if version != current:
            print(f"打包失敗。版本號 {version} 的 commit 還在本機、尚未推送；修正問題後重新執行 "
                  f"make release（或 python build.py release）會接著發行 {version}，不會重複 commit 也不會跳號。")
        raise

    # 4. 推 commit 與 tag，上傳 Release
    run(["git", "push", "origin", "main"])
    run(["git", "tag", "-a", tag, "-m", f"{NAME} {tag}"])
    run(["git", "push", "origin", tag])
    if can_publish:
        publish(tag)
    print(f"\n發行完成：{tag}")


def main():
    ap = argparse.ArgumentParser(description="打包與發行 MTool 翻譯器",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("command", nargs="?", default="build", choices=["build", "release", "publish"],
                    help="build：打包（預設）；release：本機完整發行；publish：上傳已打包的 zip")
    ap.add_argument("--onefile", action="store_true", help="打包成單一 exe（啟動較慢）")
    ap.add_argument("--skip-tests", action="store_true", help="不先跑自動化檢查（不建議）")
    ap.add_argument("--version", help="release：指定要發行的版本（例如 1.2.0）；不指定時自動把修訂版號 +1")
    ap.add_argument("--dry-run", action="store_true", help="release：只檢查與打包，不 commit、不打 tag、不上傳")
    ap.add_argument("--tag", help="publish：要上傳到哪個 tag 的 Release（例如 v1.2.0）")
    args = ap.parse_args()
    try:
        if args.command == "release":
            release(args.version, args.dry_run, args.skip_tests)
        elif args.command == "publish":
            if not args.tag:
                raise BuildError("請用 --tag 指定要上傳到哪個 Release，例如 --tag v1.2.0")
            publish(args.tag)
        else:
            build(args.onefile, args.skip_tests)
    except BuildError as e:
        sys.exit(f"\n失敗：{e}")


if __name__ == "__main__":
    main()
