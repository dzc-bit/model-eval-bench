@echo off
chcp 65001 >nul
setlocal
rem 双击本文件即启动模型评测台：起本地服务 + 自动打开浏览器。
rem 注意：本脚本不使用递归删除命令（会穿透 junction 删掉受测仓库的 node_modules），
rem       沙箱的删除全部由 console\harness\sandbox.py 以整树方式完成。
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 没有找到 Python。请先安装 Python 3.11 ~ 3.13 并加入 PATH。
    echo        本评测台只用标准库，不需要安装任何第三方依赖（但需要 pytest）。
    pause
    exit /b 1
)

python "console\server.py" --open
if errorlevel 1 (
    echo.
    echo [错误] 服务异常退出，请把上面的报错信息记下来。
    pause
)
endlocal
