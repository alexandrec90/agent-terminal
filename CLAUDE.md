# Agent Terminal

Windows terminal for Claude Code and Codex sessions, with tabs grouped by project

## Tech Stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.12 |
| Terminals | pywinpty (ConPTY), PowerShell 7 |
| Window | pywebview (WebView2), a plain-JS page with no build step |
| Terminal rendering | xterm.js, vendored in `agent_terminal/web/vendor/` |
| Page to backend | aiohttp, one loopback WebSocket with a per-run token |
| Tests | pytest |
| Lint | ruff |

## Environment Variables

See `.env.example` for every variable. `.env` is gitignored and holds this
checkout's ports and credentials.

## Tooling

> Everything in this section needs the local Docker Desktop daemon. If it isn't
> running, make the code change and defer container/stack verification until it is
> (or to CI). Run `docker ps` first — an `npipe`/daemon error means Desktop is stopped.

### Scripts and the vendored harness

Both are covered by **`.claude/rules/engineering.md`** — script conventions (pure
importable functions, stdlib-only hooks, tests in the same change), the failure-artifact
rule, and how the `.devkit.toml` seam works. That file is vendored from
[devkit](https://github.com/alexandrec90/devkit) and drift-gated, so it is the
authority; this file does not repeat it.

### VS Code tasks

- Use `"type": "process"` so VS Code monitors the process directly — that is what
  makes the spinner stop and the exit-code icon appear reliably.
- Set `"close": false` in `presentation` so the terminal stays open for review.
- **Wrap with `notify-wrap.py`** for the completion toast; never call `notify.py`
  from inside a script. Notifications are a task-layer concern only.
- Label convention: `"Domain: Title Case Action"`, and **every task carries a
  `detail`** — that is the second line in the quick-pick, and the only place a
  one-click action can state its cost or blast radius.

### Failure artifacts (fix from a file, not from the terminal)

Any task or script whose failures an agent is expected to act on must persist the
failure to a **parseable artifact file** under `logs/`. Never rely on streamed
terminal output — it scrolls away and buries the signal. Keep the terminal to a
status line plus the artifact path; put everything needed to diagnose in the file.
Write the artifact on failure too, not just success, and overwrite per run.

### Docker subprocess calls

- **`docker compose exec` must use `-T`** — without it a pseudo-TTY is allocated and
  the subprocess handle can outlive the command, leaving the caller hung.

## Testing

**`.claude/rules/engineering.md`** is the authority: tests ship in the same commit,
every testable unit of logic is covered, regression test first, targeted runs locally
and full runs in CI.

Add this project's specifics *below* — fixtures, isolation rules, markers, what to mock
and where — but do not restate the policy above. It is vendored and drift-gated; a copy
here is a fork that will disagree with it the first time either is edited.

- CI is Linux, where `winpty` and `webview` do not install. Import them only through
  `importlib` inside the function that needs them, and fake them in tests.
- A tab's status comes from OSC 7501 as the agents really send it. When an agent changes,
  capture a real session before changing `attention.py`; do not reason from its docs.
  `trust.py` and `shipping.py` are the same: both read files other tools own.
- Never write to an agent's trust record, `~/.claude.json` or `~/.codex/config.toml`: live
  sessions rewrite the first constantly, and trust is the user's decision on the agent's
  own screen. The app reads them, and only points the user at that screen.
- No test may reach the real `gh`. `gh_command` in test settings names a command that
  cannot run, which turns the PR board off, or `tests/e2e/fake_gh.py`. A real merge or
  close is outward-facing: real-repository checks are read-only.
- The `local` marker is the window itself: `tests/e2e/`, run by `scripts/e2e.py`.
- Never read a terminal's text from the DOM: WebGL paints to a canvas. Tests use
  `window.__agentTerminal.screen(id)`.

## Guardrails

Baseline guardrails — including the instruction-file feedback loop (**never silently
work around a bad instruction**) — are in `.claude/rules/engineering.md`. Rules for
writing skills and rules themselves are in `.claude/rules/authoring.md`. Cross-reference
this project's own scoped rules here, one line each.
