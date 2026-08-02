---
paths:
  - "py_modules/**/*.py"
  - "main.py"
  - "src/**/*.ts"
  - "src/**/*.tsx"
---

# Inline comments — the exception, never the default

No mechanical check exists for any of this. It holds only if it is carried at the point of writing.

Code names the **what**. A comment that restates the code is deleted on sight, not improved. If a comment is needed to
explain what the code does, the code is the problem — fix the code and drop the comment.

An inline comment earns its place only by carrying knowledge that **cannot be put into code**:

- **A fact about the outside world** — another program's behavior, a protocol quirk, a kernel or driver constraint. Name
  the source (file, symbol, version, issue) so a later reader can re-verify it rather than trust it.
- **A road not taken** — the obvious alternative and why it is wrong. Without it the next person builds it.
- **A constraint the code cannot express** — an ordering that must hold, a call that must never happen twice.

A comment that would be equally true on a `docs/` page belongs on that page, under the heading that owns the topic.
Inline is the last resort, not the first.

**When you touch a line, re-read the comment above it.** If it no longer matches, correct or delete it in the same
change. A stale comment is worse than none, because it is believed: it is read as current truth by everyone who arrives
later, and nothing in the toolchain will ever contradict it.

Avoid entirely: "mechanical extraction from X", "during the transition", "moved from Y", "added for the Z flow", "see PR
#123" — commit-message content that rots in source.

Docstrings are governed separately (`python-conventions.md`): module and class docstrings state the contract, method
docstrings may describe behavior, parameters and return value.
