"""MCP gatekeeper for the AI QA Automation Platform.

A thin front door, not an application. It authenticates the caller, validates
tool arguments, records what was attempted, and forwards to the FastAPI
backend. It contains no business logic and holds no database or Jira
credentials.

Modules:
    config: Settings. Deliberately not the backend's, so this process cannot
        learn the database password.
    schemas: Tool argument validation.
    auth: Who a call is for, and whether it may proceed. Two transports, two
        trust models.
    backend_client: The one thing this process talks to.
    audit: Recording every tool call through the backend.
    server: Tool registration and transport selection.
"""
