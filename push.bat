@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ========== 0) 检查 git ==========
git --version >nul 2>&1 || (
  echo [ERROR] 未检测到 Git，请先安装并加入 PATH。
  pause & exit /b 1
)

rem ========== 1) 切到仓库根目录 ==========
for /f "usebackq delims=" %%R in (`git rev-parse --show-toplevel 2^>nul`) do set REPO_ROOT=%%R
if not defined REPO_ROOT (
  echo [ERROR] 当前目录不是 Git 仓库，请在你的倉庫目錄運行此腳本。
  pause & exit /b 1
)
cd /d "%REPO_ROOT%"

rem ========== 2) 同步遠端（避免 push 被拒絕）==========
echo.
echo === 拉取最新更改（rebase + autostash）===
git fetch --all --prune
git -c rebase.autoStash=true pull --rebase
if errorlevel 1 (
  echo [WARN] pull 出現衝突或錯誤，請先處理。
  pause & exit /b 1
)

rem ========== 3) 生成提交信息（帶時間戳）==========
for /f "usebackq delims=" %%T in (`powershell -NoP -C "(Get-Date).ToString('yyyy-MM-dd HH:mm:ss')"`) do set TS=%%T
set MSG=%*
if "%MSG%"=="" set "MSG=auto commit %TS%"

rem ========== 4) 暫存變更 & 是否需要提交 ==========
git add -A

git diff --cached --quiet
if %errorlevel%==0 (
  echo.
  echo === 沒有檔案變更需要提交，直接嘗試推送 ===
) else (
  echo.
  echo === 提交：%MSG% ===
  git commit -m "%MSG%"
  if errorlevel 1 (
    echo [ERROR] git commit 失敗。
    pause & exit /b 1
  )
)

rem =========== 5) 推送（內建重試）============
set RETRIES=3
:push_try
echo.
echo === 推送到遠端（剩餘重試：%RETRIES%）===
git push
if not errorlevel 1 (
  goto :push_ok
)

set /a RETRIES-=1
if %RETRIES% LEQ 0 (
  echo [ERROR] push 連續失敗，請查看上方輸出。
  pause & exit /b 1
)

echo [WARN] push 失敗，重新 pull --rebase 後再試一次...
git -c rebase.autoStash=true pull --rebase
timeout /t 2 >nul
goto :push_try

:push_ok
echo.
echo === 推送成功！當前分支 & 最近一次提交 ===
git branch --show-current
git --no-pager log -1 --oneline

echo.
echo 完成。
pause