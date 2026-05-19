# Agent Instructions

This app is a read-only Interactive Brokers dashboard. Assistants should keep
the code and workflow aligned with that constraint.

## Project Checks

- Read `README.md` before making architecture or data-model changes.
- Confirm the current directory, branch, and worktree state before editing,
  committing, or merging:
  `pwd`, `git status --short --branch`, and `git worktree list`.
- This repo may use multiple worktrees. Do not assume `main` is checked out in
  the same directory as the current feature branch.
- Treat `history.sqlite3`, logs, caches, and local config as live local state.
  Do not commit them.

## IBKR Safety

- The app is display-only. Do not add calls that submit, modify, or cancel
  trades, including `placeOrder`, `cancelOrder`, or `reqGlobalCancel`.
- Be careful with order-related API calls in TWS API Read-Only mode. Prefer
  read-only state requests already used by the app, and verify behavior before
  changing open-order retrieval.
- Distinguish fresh IBKR poll results from app-cached or frontend-cached data.
  Cached data can keep the UI usable, but it should not be written as a new
  historical observation.
- Do not write incomplete account polls to history. In particular, missing Net
  Liquidation means the poll is failed/incomplete for account-history storage.

## SQLite Data

- `order_snapshots` stores compact order-state segments. Do not reintroduce
  row-per-poll order storage.
- `executions` are the authoritative ledger for fill time, fill quantity, and
  History attribution.
- `order_snapshots_legacy` is a rollback table from the compact-order
  migration. Do not drop it or run `VACUUM` without explicit user approval.
- Do not delete or rewrite live SQLite data unless the user explicitly asks for
  that data operation.
- Future storage work should focus on reducing duplicate rows in large
  time-series tables such as `position_snapshots` and `contract_snapshots`
  while preserving reconstructable historical values.

## Tests

- For storage, attribution, order, or History changes, run:

```sh
PYTHONPYCACHEPREFIX=/tmp/ib_pycache /Users/nick/anaconda3/bin/python -m unittest discover -s tests
```

- If a change intentionally skips tests, explain why in the final response.

## Commits

- Use action-oriented commit subjects.
- Keep commit-message lines wrapped around 72-80 characters.
- Include this trailer on Codex-authored commits:

```text
AI-Assisted-by: Codex; model=GPT-5.5
```

- If another assistant authored the changes, use that assistant and model in
  the trailer instead.
