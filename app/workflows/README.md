# app/workflows (placeholder)

**Status:** empty. Filled in **Phase 3**.

## Purpose
LangGraph workflow definitions that chain the modules together, for example:
issue -> BRD -> test cases -> approval -> automation -> execution -> report.

## Rules
A workflow node calls a module's `service.py`. It does not reach into
repositories or hold business logic of its own.
