# Agent Terminal

A Windows terminal for Claude Code and Codex sessions: tabs grouped by project down a
sidebar instead of along a line, a tab that tells you when its agent needs you, and the
PRs those sessions become, reviewed and merged from the same window.

Each tab runs PowerShell 7 in a real pseudo-console with the agent inside it, so leaving
the agent leaves you at a shell in the same place.

## Run it

```bash
uv sync --all-extras
.venv\Scripts\agent-terminal.exe        # no console window
```

Starting it a second time brings the running window forward.

## Using it

| To | Do |
| --- | --- |
| Start a session | **+** on a group, or **+ New group** for any folder: a project, a pasted path, or **Browse…** |
| See who needs you | An amber tab, which blinks unless it's the one you're on |
| See what has finished | A blue tab. It holds still, and can wait as long as you like |
| Leave a tab for later | Right-click it. It greys out; right-click again to undo |
| Close a session | **×** on the tab |
| Fold a group | Click its heading; folded, it shows a count, amber or blue if a session wants you |
| Close a group | **×** on its heading (on hover): closes every session in it after asking, and hides its PRs until a session opens there |
| Clone a repository | **+ New group** → **Clone one of your GitHub repositories…**: lists yours not yet in the projects root |
| Delete a project | The bin beside it in **+ New group**. It goes to the Recycle Bin, after a warning naming anything not pushed; one with a tab open is refused |
| Put a new line in a prompt | Shift+Enter or Ctrl+Enter |
| Copy / paste in a terminal | Right-click: copies a selection, pastes if there is none |
| Review a PR | Click its chip on a tab, or its row in a group |
| Merge or close a PR | **Merge** or **Close PR** in the review; both ask first |
| Reopen a PR's session | **Open prompt** in the review, when its worktree still holds one |

Amber means the agent cannot go on without you: it asked for permission, asked a
question, failed, or is on a trust screen. Blue means it finished a turn. A grey tab
stays grey until the agent stops on you *again*.

Closing the window ends the agents and keeps the tabs: the next launch resumes every
session, with its worktree, grey state and selected tab. **×** forgets a tab for good. It
does not remove a worktree the session created, since that may hold work.

### Updating without stopping the agents

Each tab's process lives in a **session host**, a small background process the app starts
(`agent_terminal/host.py`), not in the window. A pseudo-console ends with the process
that made it, so this is what lets the window go away and come back while an agent is
mid-turn. The host keeps each tab's recent output (about two million characters), and
the window that comes back rebuilds each tab's screen from it.

| The bar above the groups says | Do |
| --- | --- |
| `Update available: 3 commits.` | **Update**: `git pull --ff-only` and `uv sync`, then the window restarts. Agents carry on |
| the update is blocked, and why | Nothing to click: the checkout is on another branch, has local commits, or is dirty |
| `Updated: abc123 → def456` | The last update worked |
| `Update failed at …` | It names the step; the full output is in `update.log` in the data directory |
| `Running sessions use the previous session host.` | The update changed the host itself. **Restart sessions** (two clicks) ends every agent and resumes each one |

The app checks `origin/main` at start and every half hour, from the checkout it runs
from. Closing the window still ends everything, host included. If the window crashes,
the host keeps the agents for an hour (`AGENT_TERMINAL_HOST_ORPHAN_SECONDS`) for the next
launch to take back. If the host cannot start, the app runs the agents itself as it did
before, and a restart ends them.

### Starting somewhere new

The dialog says what will happen before you press Start. In a folder an agent has not
seen, it asks you to trust it on its own screen: choosing **Yes** in a Claude worktree tab
carries on into the worktree without another click, because `claude -w` itself refuses
instead of asking. Nothing here grants trust for you.

A worktree needs a repository with a commit. In a folder without one the dialog offers
**Initialize git and commit this folder**, which stages everything there, so look at what
is in it first. Nothing is pushed.

### PRs

Every repository under the projects root, and every one a tab is in, is asked about its
open PRs each minute. So a project with an open PR has a group even with no session in it.

| A tab shows | Meaning |
| --- | --- |
| nothing | A prompt with nothing shipped yet |
| `ship queued` | `logs/ship-intent.md` is waiting for the fix pass |
| `ship refused` | The pass tried that intent and could not ship it |
| `PR #12 ✓` | Its PR is open; `✗` checks failing, `…` running, `conflicts` needs a rebase |
| `PR #12 merged` / `closed` | Its PR is done, and the tab is yours to close |
| **Create PR** | It has changes and no PR: click to have its agent run devkit's `land` skill |
| **Push** | Its PR is open and it has changes the PR has not: the same skill adds them |
| **New PR** | Its PR merged and it has changes since: its agent ships those alone as a new one |

The button needs the project to have the `land` skill, and works while the agent is at its
prompt. It types there, so anything already typed is sent along with it.

Beside a PR, an icon for each thing that judges it, gray until it has and then green or red:
CI (GitHub's checks), local tests and E2E (devkit's scheduled tiers, which run after the
merge, so a red may be another change's doing), and live. Live is green once the main
checkout has the merge, or for this app once it has restarted on it. A type the project
does not have shows no icon.

A PR no tab holds is a row of its own in its group, marked `prompt on disk` if the
worktree it came from still has a session to reopen. A tab and a PR are matched by the
branch checked out in the tab's folder, so this works for sessions started outside the
app, and follows a branch the fix pass renames.

Merging squashes, as the harness's own merges do. It does not delete the branch: the
repository's delete-on-merge setting covers the remote, and `worktree.py reconcile` owns
the worktree.

## How a tab knows

No agent hook is involved. Both CLIs report their state to the terminal with an OSC 7501
sequence, and Claude only does so once the terminal answers its `7501;?` query.
`agent_terminal/attention.py` reads a copy of the output for those and answers the query;
the sequences themselves were captured from real sessions, not taken from a spec.

Claude takes a session id on its command line, so its tabs can always resume. Codex
chooses its own and writes it to `~/.codex/sessions/`, so a Codex tab finds its session
there by start time and directory (`agent_terminal/codex_sessions.py`). Two Codex sessions
started in one project at the same moment, one of them outside this app, could be
matched to the wrong tab.

## Develop

```bash
python scripts/run-tests.py     # the tests for what changed
python scripts/e2e.py           # the real window, driven over DevTools (needs a desktop)
```

`tests/e2e/` runs the real app with `tests/e2e/fake_agent.py` in place of an agent CLI and
`tests/e2e/fake_gh.py` in place of `gh`, so no test can touch a real PR. The suite proper
fakes the pseudo-console, because CI is Linux.

## Layout

```text
agent_terminal/              the app
  attention.py               what a tab's output says about its agent
  agents.py                  how each agent is launched and resumed
  codex_sessions.py          finding a Codex tab's session on disk
  runner.py                  a tab's process; pty_session.py is the pseudo-console
  sessions.py                where a tab's process lives: here, or in the session host
  host.py                    the session host, which outlives the window
  host_client.py             the app's side of it; host_protocol.py is the wire format
  updates.py                 checking for an update, and the restart the page asks for
  relaunch.py                the helper that updates the checkout and starts the app again
  detached.py                starting a process that outlives the app
  manager.py                 the tabs, and every change the page can ask for
  desktop.py                 what the page asks of the machine outside its tabs
  folders.py                 scoping a tab to any folder
  gitinfo.py                 what a folder is to git, mostly read straight from .git
  trust.py                   reading whether an agent trusts a folder
  prs.py                     pull requests through `gh`
  pr_board.py                keeping PRs current and tying them to tabs
  prompts.py                 finding the session that worked in a worktree
  shipping.py                where a worktree stands with the fix pass
  server.py                  the loopback server the window's page talks to
  store.py                   the session file
  web/                       the page; web/vendor/ is xterm.js
tests/                       tests; tests/e2e/ is the local tier
scripts/                     project scripts (Python, each with tests)
scripts/hooks/               vendored agent harness — edit upstream in devkit
.devkit.toml                 the per-project harness seam (NOT vendored)
```

## CI

`.github/workflows/pr-gate.yml` runs lint, tests, and the harness drift check on
every PR. The drift check is only meaningful when it can see devkit — if it prints
"nothing to do (skipping)", the gate is inert and the wiring needs fixing. Locally,
where `DEVKIT_DIR` is usually unset, `pre-commit run devkit-drift --all-files` is the
same check against the devkit rev pinned in `.pre-commit-config.yaml`.

Tests a hosted runner cannot run are marked `local`, which plain `pytest` and the gate
deselect. `python scripts/e2e.py` runs that tier here, and devkit's nightly
`devkit-local-e2e` job runs it on the default branch; a red run reaches the fix pass.

`.github/dependabot.yml` opens weekly dependency PRs, and
`.github/workflows/dependabot-automerge.yml` merges every one of them once the gate
passes, majors included: the gate is the review, so keep it covering what you care
about. To hold one bump back, close it or comment `@dependabot ignore this major
version`: a removed `automerge` label comes back on Dependabot's next rebase.

That auto-merge workflow is **vendored** from devkit, so `sync-devkit.py --check`
compares it byte-for-byte and editing it here shows up as drift. Keep the gate's title
`PR Gate`: the auto-merge workflow waits on it by name, and a rename makes the merge job
inert without turning anything red.

`.github/actions/setup-python-env/` is **yours**, by contrast. devkit shipped it through
that same channel for one release and pulled it back out when two projects turned out to
install in ways `uv sync` cannot express, so it now arrives from `templates/` as a
one-shot copy: nothing compares it, and rewriting its steps to match how this project
installs is the supported move rather than drift. What owning it costs is the other
direction — later devkit fixes to that file never reach here.
