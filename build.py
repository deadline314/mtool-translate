"""
打包成 Windows 程式，所有打包設定都在這支腳本裡（不需要維護 .spec 檔）：

    python build.py              資料夾版（預設，啟動快）：dist/MTool翻譯器/MTool翻譯器.exe，另附 zip 方便傳送
    python build.py --onefile    單一 exe：dist/MTool翻譯器.exe（好傳送，但每次啟動要先解壓，比較慢）
    python build.py --skip-tests 不先跑自我檢查（不建議）

流程：安裝打包工具 → 跑自動化檢查 → 打包 → 用打包好的程式跑 --self-check，任何一步失敗就停止。
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "MTool翻譯器"
ICON = os.path.join(ROOT, "assets", "app.ico")
# 程式用不到、但可能被其他套件順帶拉進來的大型模組，排除可縮小體積、加快啟動
EXCLUDES = ["tests", "tools", "unittest", "pydoc", "doctest", "PIL", "numpy", "pytest", "IPython"]


def run(cmd):
    print("$", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def main():
    ap = argparse.ArgumentParser(description="打包 MTool 翻譯器")
    ap.add_argument("--onefile", action="store_true", help="打包成單一 exe（啟動較慢）")
    ap.add_argument("--skip-tests", action="store_true", help="不先跑自動化檢查")
    args = ap.parse_args()

    run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "pyinstaller", "-r", "requirements.txt"])
    if not args.skip_tests:
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."])

    dist, work = os.path.join(ROOT, "dist"), os.path.join(ROOT, "build")
    target = os.path.join(dist, NAME + (".exe" if args.onefile else ""))
    if os.path.exists(target):
        shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--onefile" if args.onefile else "--onedir",
           "--name", NAME, "--icon", ICON, "--add-data", f"{ICON}{os.pathsep}assets",
           "--distpath", dist, "--workpath", work, "--specpath", work]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    run(cmd + [os.path.join(ROOT, "app.py")])

    exe = target if args.onefile else os.path.join(target, NAME + ".exe")
    exe_dir = os.path.dirname(exe)
    code = subprocess.run([exe, "--self-check"]).returncode
    report = os.path.join(exe_dir, "self-check.txt")
    with open(report, encoding="utf-8") as f:
        print(f.read())
    os.remove(report)
    if code != 0:
        sys.exit("打包後的自我檢查沒有通過，請看上面的報告")

    if not args.onefile:
        zip_path = shutil.make_archive(os.path.join(dist, NAME), "zip", dist, NAME)
        print(f"zip：{zip_path}")
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(target) for f in fs) \
        if os.path.isdir(target) else os.path.getsize(target)
    print(f"完成：{exe}（{size / 1e6:.1f} MB）")


if __name__ == "__main__":
    main()
