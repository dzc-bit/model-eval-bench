@echo off
chcp 65001 >nul
setlocal
rem 双击本文件即启动模型评测台：起本地服务 + 自动打开浏览器。
rem 注意：本脚本不使用递归删除命令（会穿透 junction 删掉受测仓库的 node_modules），
rem       沙箱的删除全部由 console\harness\sandbox.py 以整树方式完成。
cd /d "%~dp0"

rem ── 用哪个解释器不是随便的 ────────────────────────────────────────────
rem 评分是以"启动服务的那个解释器"起 pytest 子进程的（checks/pytest.py 用
rem sys.executable）。所以受测仓库声明的依赖（pandas / numpy / pyarrow / duckdb /
rem akshare / pydantic / openai …）必须装在那个解释器里，否则 316 个用例会在
rem 收集阶段全灭，分数直接是 0。
rem .venv-gate 就是为此而建；有它就优先用，没有才退回系统 python 并明确警告。
set "VENV=.venv-gate\Scripts\python.exe"
set "PY=python"
if exist "%VENV%" set "PY=%VENV%"

if exist "%VENV%" goto :run

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 没有找到 Python。请先安装 Python 3.11 ~ 3.13 并加入 PATH。
    echo        评测台本体只用标准库；但受测仓库需要它自己那套依赖，
    echo        建议创建 venv 后安装：见 README 第 3 节。
    pause
    exit /b 1
)
echo [警告] 未找到 .venv-gate，本次退回系统 python。
echo        核心题（T1-01..T4-11）可能因缺少受测仓库依赖而无法校验；
echo        只有 DEMO-01 不依赖外部包。
echo.

:run
echo [启动] 使用解释器：%PY%
"%PY%" "console\server.py" --open
if errorlevel 1 (
    echo.
    echo [错误] 服务异常退出，请把上面的报错信息记下来。
    pause
)
endlocal
