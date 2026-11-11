# What AGENT_TERMINAL_CLAUDE_COMMAND and _CODEX_COMMAND point at in the end-to-end tests:
# a command a PowerShell tab can invoke by path, as it does the real `claude`.
& $env:FAKE_AGENT_PYTHON (Join-Path $PSScriptRoot 'fake_agent.py') @args
