# Errors

## ERR-20261008-001
- Status: resolved
- Priority: low
- Area: environment
- Default shell and Node REPL failed to start with sandbox setup refresh errors.
- A reviewed elevated read-only shell succeeded; scoped repository edits could proceed.


## 2026-10-12 — 项目测试运行环境

默认系统 Python 未包含项目依赖；应使用 `.venv/Scripts/python`。当前 Starlette 的 TestClient 还需要本地测试依赖 httpx2，已安装在项目虚拟环境，未加入生产依赖。
