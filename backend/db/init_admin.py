"""Bootstrap the first admin user for a fresh deployment.

Reads from environment variables — NEVER takes the password from the command
line (argv leaks into shell history and the process list). The password lives
only in your cloud provider's secret manager.

Env vars (required for first-run bootstrap):
    APP_ADMIN_USERNAME   — e.g. "admin" (no spaces, no @)
    APP_ADMIN_PASSWORD   — the initial password. Minimum 10 chars.

Optional:
    APP_ADMIN_EMAIL      — used only for display
    APP_ADMIN_FULLNAME   — used only for display

Behavior:
    * If the username already exists and is an admin: no-op (idempotent).
    * If the username exists but has a different role: refuse, print a message
      telling the operator to promote the user via the API instead.
    * If no user exists and the env vars are missing: refuse with an error
      listing which variables need to be set.

Usage:
    # Local development
    set APP_ADMIN_USERNAME=admin
    set APP_ADMIN_PASSWORD=<your chosen password>
    python -m backend.db.init_admin

    # Cloud (AWS ECS / Azure Container Apps / GCP Cloud Run)
    # Wire the env vars to your secrets store, then run once as a bootstrap
    # task / job against the same database the web service will use.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from sqlalchemy import select

from backend.db.database import Base, UserAccount, async_session, engine
from backend.security import hash_password

log = logging.getLogger("init_admin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")


async def ensure_admin(force_reset: bool = False) -> int:
    username = os.getenv("APP_ADMIN_USERNAME", "").strip()
    password = os.getenv("APP_ADMIN_PASSWORD", "")
    email = os.getenv("APP_ADMIN_EMAIL", "").strip() or None
    full_name = os.getenv("APP_ADMIN_FULLNAME", "").strip() or None

    if not username or not password:
        missing = [
            v for v, set_ in [
                ("APP_ADMIN_USERNAME", bool(username)),
                ("APP_ADMIN_PASSWORD", bool(password)),
            ] if not set_
        ]
        print(
            f"error: missing required env var(s): {', '.join(missing)}\n"
            "       these are only read from the environment — never pass them on the command line.",
            file=sys.stderr,
        )
        return 2

    if len(password) < 10:
        print("error: APP_ADMIN_PASSWORD must be at least 10 characters", file=sys.stderr)
        return 2

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        q = await session.execute(select(UserAccount).where(UserAccount.username == username))
        existing = q.scalar_one_or_none()

        if existing is None:
            user = UserAccount(
                username=username,
                full_name=full_name,
                email=email,
                password_hash=hash_password(password),
                role="admin",
                disabled=False,
            )
            session.add(user)
            await session.commit()
            log.info("Bootstrapped admin user %r", username)
            return 0

        if existing.role != "admin" and not force_reset:
            print(
                f"error: user {username!r} already exists with role {existing.role!r}. "
                f"Refusing to silently promote — use the API or pass --force-reset.",
                file=sys.stderr,
            )
            return 3

        # Existing admin — optionally reset password for recovery scenarios.
        if force_reset:
            existing.role = "admin"
            existing.disabled = False
            existing.password_hash = hash_password(password)
            existing.password_changed_at = datetime.utcnow()
            await session.commit()
            log.info("Reset password + role for existing user %r", username)
            return 0

        log.info("Admin user %r already exists; no change (idempotent).", username)
        return 0


def main():
    p = argparse.ArgumentParser(description="Bootstrap the initial admin user.")
    p.add_argument("--force-reset", action="store_true",
                   help=("Existing user with the same name? Overwrite password and "
                         "reassert admin role. Use for recovery only."))
    args = p.parse_args()

    rc = asyncio.run(ensure_admin(force_reset=args.force_reset))
    sys.exit(rc)


if __name__ == "__main__":
    main()
