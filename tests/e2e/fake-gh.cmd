@echo off
rem What AGENT_TERMINAL_GH_COMMAND points at in the end-to-end tests.
"%FAKE_AGENT_PYTHON%" "%~dp0fake_gh.py" %*
