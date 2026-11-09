"""Operator CLI for seeding users and project access.

Phase 1 has no admin API, so this is the only way rows get into ``users`` and
``user_project_access``. Without at least one user holding one grant, every
call to ``GET /api/v1/issues/{issue_key}`` is refused — correctly, but
unhelpfully.

A script rather than HTTP endpoints, deliberately. Write endpoints would need
an authorisation model for administrators, which the brief defers to a later
phase, and they would be reachable by anything that could reach the API. A
command needs shell access to the host or container, which is already the
boundary that protects the database.

Run it from the project root:

    uv run python -m app.cli list-users
    uv run python -m app.cli add-user --email qa@example.com --name "QA One"
    uv run python -m app.cli grant-access --email qa@example.com --project ABC

Or inside the container, where the environment is already set:

    docker compose exec api python -m app.cli list-users

Every command prints what it did and exits non-zero on failure, so it is safe
to use in a provisioning script.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import dispose_engine, get_session_factory
from app.core.logging import configure_logging
from app.models.user import PROJECT_KEY_PATTERN, User, UserProjectAccess, UserRole

_PROJECT_KEY_RE = re.compile(PROJECT_KEY_PATTERN)


def _normalise_email(raw: str) -> str:
    """Lowercase and strip an email.

    ``users.email`` is stored lowercase with a CHECK enforcing it, so an
    operator typing a mixed-case address would otherwise hit a constraint
    violation and a confusing error.
    """
    return raw.strip().lower()


def _validate_project_key(raw: str) -> str:
    """Return an upper-cased project key, or exit with a readable message.

    Checked here as well as by the database constraint so the operator gets
    "ABC is the shape I need" rather than a Postgres CHECK violation.
    """
    key = raw.strip().upper()
    if not _PROJECT_KEY_RE.match(key):
        sys.exit(
            f"error: {raw!r} is not a Jira project key. Expected uppercase letters "
            "and digits, starting with a letter, for example ABC."
        )
    return key


async def _require_user(session: AsyncSession, email: str) -> User:
    """Load a user by email, or exit telling the operator how to create one."""
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        sys.exit(f"error: no user with email {email}. Create one with 'add-user' first.")
    return user


async def add_user(email: str, name: str, role: str) -> int:
    """Create a user, or report that one already exists."""
    email = _normalise_email(email)
    async with get_session_factory()() as session:
        user = User(email=email, display_name=name.strip(), role=UserRole(role))
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await _require_user(session, email)
            print(f"already exists: {existing.email} ({existing.role.value})")
            return 0
        print(f"created user {user.email} with role {user.role.value}")
    return 0


async def grant_access(email: str, project: str) -> int:
    """Grant one user read access to one project."""
    email = _normalise_email(email)
    project_key = _validate_project_key(project)
    async with get_session_factory()() as session:
        user = await _require_user(session, email)
        session.add(UserProjectAccess(user_id=user.id, project_key=project_key))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            print(f"already granted: {email} -> {project_key}")
            return 0
        print(f"granted {email} access to {project_key}")
    return 0


async def revoke_access(email: str, project: str) -> int:
    """Remove a grant. Does not touch the audit trail."""
    email = _normalise_email(email)
    project_key = _validate_project_key(project)
    async with get_session_factory()() as session:
        user = await _require_user(session, email)
        result = await session.execute(
            delete(UserProjectAccess).where(
                UserProjectAccess.user_id == user.id,
                UserProjectAccess.project_key == project_key,
            )
        )
        await session.commit()
        if result.rowcount:
            print(f"revoked {email} access to {project_key}")
        else:
            print(f"no grant to revoke: {email} -> {project_key}")
    return 0


async def set_active(email: str, *, active: bool) -> int:
    """Activate or deactivate a user.

    Deactivating is the supported alternative to deletion: it stops the
    account being used while keeping their jobs and audit rows attributable.
    """
    email = _normalise_email(email)
    async with get_session_factory()() as session:
        user = await _require_user(session, email)
        user.is_active = active
        await session.commit()
        print(f"{'activated' if active else 'deactivated'} {email}")
    return 0


async def list_users() -> int:
    """Print every user with their role, state and granted projects."""
    async with get_session_factory()() as session:
        users = (await session.execute(select(User).order_by(User.email))).scalars().all()
        if not users:
            print("no users. Create one with 'add-user'.")
            return 0
        grants = (
            await session.execute(
                select(UserProjectAccess.user_id, UserProjectAccess.project_key).order_by(
                    UserProjectAccess.project_key
                )
            )
        ).all()
        by_user: dict[object, list[str]] = {}
        for user_id, project_key in grants:
            by_user.setdefault(user_id, []).append(project_key)

        print(f"{'EMAIL':40} {'ROLE':13} {'ACTIVE':7} PROJECTS")
        for user in users:
            projects = ", ".join(by_user.get(user.id, [])) or "-"
            print(f"{user.email:40} {user.role.value:13} {user.is_active!s:7} {projects}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Seed and manage platform users and their Jira project access.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-user", help="create a user")
    add.add_argument("--email", required=True)
    add.add_argument("--name", required=True, help="display name")
    add.add_argument(
        "--role",
        default=UserRole.VIEWER.value,
        choices=[member.value for member in UserRole],
        help="defaults to viewer, the least privilege",
    )

    grant = sub.add_parser("grant-access", help="grant read access to a Jira project")
    grant.add_argument("--email", required=True)
    grant.add_argument("--project", required=True, help="project key, for example ABC")

    revoke = sub.add_parser("revoke-access", help="remove a project grant")
    revoke.add_argument("--email", required=True)
    revoke.add_argument("--project", required=True)

    for name, help_text in (
        ("deactivate-user", "stop an account being used, keeping its history"),
        ("activate-user", "re-enable a deactivated account"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--email", required=True)

    sub.add_parser("list-users", help="show users, roles and their granted projects")
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    """Route a parsed command to its handler."""
    match args.command:
        case "add-user":
            return await add_user(args.email, args.name, args.role)
        case "grant-access":
            return await grant_access(args.email, args.project)
        case "revoke-access":
            return await revoke_access(args.email, args.project)
        case "deactivate-user":
            return await set_active(args.email, active=False)
        case "activate-user":
            return await set_active(args.email, active=True)
        case "list-users":
            return await list_users()
        case _:  # pragma: no cover  # argparse rejects anything else
            return 2


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the command, and always release the pool."""
    args = _build_parser().parse_args(argv)
    # WARNING, not INFO: this is an interactive tool, and "creating database
    # engine" between the prompt and the output is noise.
    configure_logging(level="WARNING", service="qa-platform-cli")

    async def run() -> int:
        try:
            return await _dispatch(args)
        finally:
            await dispose_engine()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
