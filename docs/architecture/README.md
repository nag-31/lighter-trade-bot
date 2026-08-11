# Architecture docs

Two views of the same system, in two folders:

| Folder | For | Read if… |
| --- | --- | --- |
| [`easy/`](easy/) | You (the human) | you want to understand how it works and what to watch for, without code |
| [`detailed/`](detailed/) | The agent (and future you) | you need file-level maps, invariants, and audit pointers |

Pre-rendered PNG diagrams live in [`diagrams/`](diagrams/). The `.mmd`
source files are also there — they render automatically on GitHub and in
VS Code with the Mermaid extension.

## Start here

1. **I want the big picture** → [`easy/OVERVIEW.md`](easy/OVERVIEW.md) (system diagram + plain-English story)
2. **I want to know if something is broken** → [`easy/CHECKLIST.md`](easy/CHECKLIST.md) (what good looks like, what to flag)
3. **I want every file explained** → [`detailed/MODULE_MAP.md`](detailed/MODULE_MAP.md)
4. **I want the accounting rules** → [`detailed/ACCOUNTING.md`](detailed/ACCOUNTING.md)
5. **I want the data flow step by step** → [`detailed/DATA_FLOW.md`](detailed/DATA_FLOW.md)
6. **I want to know what to not break** → [`detailed/INVARIANTS.md`](detailed/INVARIANTS.md)
