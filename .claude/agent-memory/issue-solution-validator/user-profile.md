---
name: user-profile
description: User role, workflow preferences, and collaboration style for DocumentReader project
metadata:
  type: user
---

The user works on a Python desktop PDF reader (DocumentReader) and uses a three-agent workflow:
1. `bug-detective` — finds bugs and writes to `issues.md`
2. `issue-fixer` — reads `issues.md`, implements fixes
3. `issue-solution-validator` (this agent) — validates each fix through tests + inspection, updates `issues.md`

The user appears to be a developer comfortable with Python, threading, Windows platform specifics (COM, MCI), and Tkinter. They run all three agents in sequence as a structured quality workflow. Issues are tracked in `issues.md` with structured sections: Discovery, Fix, and Validation.

**How to apply:** Provide technical depth in validation reports. Be precise about which line numbers contain the fix. Distinguish between test infrastructure defects and real code defects clearly.
