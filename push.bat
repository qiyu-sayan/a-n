@echo off
cd /d "%~dp0"
echo ============================================
echo 🚀 一键推送脚本启动
echo ============================================

:: 检查是否为 git 仓库
if not exist ".git" (
    echo ❌ 当前目录不是 Git 仓库！
    pause
    exit /b
)

:: 自动添加所有变更
echo 🟡 正在添加文件...
git add -A

:: 自动生成提交信息（带时间戳）
setlocal enabledelayedexpansion
for /f "tokens=1-3 delims=/ " %%a in ('date /t') do set today=%%a-%%b-%%c
for /f "tokens=1 delims= " %%a in ('time /t') do set now=%%a
set msg=auto commit !today! !now!

echo 🟢 正在提交：!msg!
git commit -m "!msg!" >nul 2>&1

:: 推送
echo 🟣 正在推送到远程仓库...
git push origin main

echo ============================================
echo ✅ 推送完成！
echo ============================================
pause