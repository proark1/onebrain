## Shipping workflow

When a requested development task is complete, automatically ship it unless the user explicitly says not to.

Default flow:
1. Run the relevant checks.
2. Stage only task-related files.
3. Commit with a concise message.
4. Push the working branch.
5. Switch to `main`.
6. Pull `main` with `--ff-only`.
7. Merge the working branch into `main`.
8. Push `main`.

Do not auto-ship if checks fail, merge conflicts occur, unrelated local changes are present, secrets are detected, or the user asks for review/report/planning only.
