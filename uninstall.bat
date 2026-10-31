@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  DSH Cookie Cleaner - 卸载 / 清理数据
echo ============================================================
echo.

if not exist "DSHCookieCleaner.exe" (
    echo [警告] 当前目录没有找到 DSHCookieCleaner.exe，
    echo        这里可能不是程序所在文件夹。
    echo        为避免误删其他文件，卸载已停止。
    echo.
    if not "%DSH_BUILD_NO_PAUSE%"=="1" pause
    exit /b 1
)

echo 将删除本文件夹内的：
echo   - config.json（设置）
echo   - log 文件夹（日志）
echo   - backups 文件夹（Cookie 备份）
echo.
echo 注意：如果你在设置里把日志或备份改到了其他盘符/文件夹，
echo       脚本无法跨盘删除，请手动删除那些自定义目录。
echo.
set /p "CONFIRM=确定要完全卸载并清理所有数据吗？(Y/N) "
if /i not "%CONFIRM%"=="Y" (
    echo.
    echo 已取消，未删除任何文件。
    if not "%DSH_BUILD_NO_PAUSE%"=="1" pause
    exit /b 0
)

echo.
echo 正在删除设置、日志和备份...
if exist "config.json" del /q "config.json"
if exist "log\" rmdir /s /q "log"
if exist "backups\" rmdir /s /q "backups"

rem 清除开机自启项；不存在时忽略错误。
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "DSHCookieCleaner" /f >nul 2>nul

echo 已清理本文件夹内的配置、日志和备份。
echo.
echo 程序本体请直接删除本文件夹即可。
echo.
if not "%DSH_BUILD_NO_PAUSE%"=="1" pause
endlocal
exit /b 0
