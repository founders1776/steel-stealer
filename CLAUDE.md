### CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Objective

Steel Stealer — We are going to build a scraper to import products from our vacuum supplier, Steel City Vacuum, into our shopify store. We need to log into the website to gain access to the prodcuts.

# User Info:
Steel City credentials are stored in environment variables (SC_ACCOUNT, SC_USER, SC_PASSWORD).
See .env for local runs, or GitHub Secrets for CI.

# How we Know We Have Done Our Job
We go to the "Schematics" tab on Steel City's main portal (once behind the "login" wall) and we click on each brand and downlaod all the parts that are listed for EACH machine model's schematic (they should be linked right within the schematic), including product description, part numbers/skus, pricing, and any other relevant information. Leave the photos off. We just need to build an excel sheet for this

## AFTER EACH UPDATE TO THIS SCRIPT
you will create a file in this directory called "Schematics.md," which shows all the wiring and relevant files for this script. You will update and edit this file after every change made to this script. This will be a complex script, so this is imperative. 

## USEFUL INFO
Update this CLAUDE.md file to give yourself better instructions as we learn more about the project

## Workflow Orchestration

1. **Plan Mode Default** — Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions). If something goes sideways, STOP and re-plan immediately — don't keep pushing. Use plan mode for verification steps, not just building. Write detailed specs upfront to reduce ambiguity.

2. **Subagent Strategy** — Use subagents liberally to keep main context window clean. Offload research, exploration, and parallel analysis to subagents. For complex problems, throw more compute at it via subagents. One task per subagent for focused execution.

3. **Self-Improvement Loop** — After ANY correction from the user: update `tasks/lessons.md` with the pattern. Write rules for yourself that prevent the same mistake. Ruthlessly iterate on these lessons until mistake rate drops. Review lessons at session start for relevant project.

4. **Verification Before Done** — Never mark a task complete without proving it works. Diff behavior between main and your changes when relevant. Ask yourself: "Would a staff engineer approve this?" Run tests, check logs, demonstrate correctness.

5. **Demand Elegance (Balanced)** — For non-trivial changes: pause and ask "is there a more elegant way?" If a fix feels hacky: "Knowing everything I know now, implement the elegant solution." Skip this for simple, obvious fixes — don't over-engineer. Challenge your own work before presenting it.

6. **Autonomous Bug Fixing** — When given a bug report: just fix it. Don't ask for hand-holding. Point at logs, errors, failing tests — then resolve them. Zero context switching required from the user. Go fix failing CI tests without being told how.

## Task Management

- **Plan First**: Write plan to `tasks/todo.md` with checkable items
- **Verify Plan**: Check in before starting implementation
- **Track Progress**: Mark items complete as you go
- **Explain Changes**: High-level summary at each step
- **Document Results**: Add review section to `tasks/todo.md`hm 
- **Capture Lessons**: Update `tasks/lessons.md` after corrections

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.