---
name: migrate-app
description: Move a user's existing script (Python or PowerShell) into a pdt app that runs on a schedule. Use when someone wants to migrate, move, or schedule a script they already have with pdt.
---

# Migrate a script to a pdt app

First, gather the information required to help the user.

## 1. Gather information

<!-- TODO: questions, one at a time -->
- Where is the script, and what language is it?
- What does it do, and how often should it run?
- What secrets, files, or network access does it need?
- How does the user run it today?

## 2. Create the app

<!-- TODO -->
- A PowerShell script: copy the `.ps1` files into `APP/`, then run `pdt new APP --from-scripts`.
- A Python script: start from the example that is nearest to it (`pdt examples`, then `pdt new APP --from EXAMPLE`).

## 3. Settings and secrets

<!-- TODO: schedule, env vars, .env -->

Check: `pdt validate` reports no problems.

## 4. Run it on this computer

Check: `pdt run APP` succeeds, and its output matches what the old script produced.

## 5. Deploy

Return to step 5 of the `pdt-onboarding` skill.
