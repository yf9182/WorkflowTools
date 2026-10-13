#!/bin/sh
set -eu
task_root="$(cd "$(dirname "$0")/.." && pwd)"
task_python="${WORKFLOW_TOOLS_PYTHON:-python3}"
"$task_python" -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11+"'
if [ ! -x "$task_root/.venv/bin/python" ]; then
  "$task_python" -m venv "$task_root/.venv"
fi
"$task_root/.venv/bin/python" -m pip install "$task_root[mcp]"
printf '%s\n' "安装完成：$task_root/bin/workflow-tools install --project <项目目录>"
