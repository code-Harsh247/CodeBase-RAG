"""`codegraph admin ...`: operate the public demo from the server's shell.

Reads the same ``APP_DB_PATH`` as the running server. SQLite in WAL mode lets
both open it at once; every command here is a short transaction.
"""

from __future__ import annotations

import argparse
import time

from api.maintenance import physical_delete, reconcile
from api.settings import Settings
from api.store import AppStore
from graph.neo4j_client import Neo4jClient


def register(subparsers) -> dict:
    admin = subparsers.add_parser("admin", help="manage users and repositories of the web app")
    commands = admin.add_subparsers(dest="admin_command", required=True)

    commands.add_parser("repos", help="list every indexed repository")
    commands.add_parser("users", help="list users, most recently seen first")
    commands.add_parser(
        "reconcile", help="adopt graph repos missing from the store; flag vanished ones"
    )

    delete = commands.add_parser("delete", help="delete a repository for everyone")
    delete.add_argument("repo_id")

    public = commands.add_parser("public", help="make a repository visible to all users")
    public.add_argument("repo_id")
    public.add_argument("--off", action="store_true", help="make it private again")

    for name, text in (("ban", "suspend a user"), ("unban", "lift a suspension")):
        command = commands.add_parser(name, help=text)
        command.add_argument("login")

    return {"admin": _dispatch}


def _store() -> AppStore:
    return AppStore(Settings.from_env().app_db_path)


def _when(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(timestamp))


def _dispatch(args: argparse.Namespace) -> int:
    store = _store()
    try:
        return _HANDLERS[args.admin_command](args, store)
    finally:
        store.close()


def _repos(args, store: AppStore) -> int:
    records = store.list_repos()
    for r in records:
        flag = "public " if r.public else "private"
        print(
            f"{r.repo_id:<40} {r.status:<8} {flag} owners={r.owner_count:<3} "
            f"nodes={r.nodes:<7} used {_when(r.last_used_at)}"
        )
    print(f"({len(records)} repositories)")
    return 0


def _users(args, store: AppStore) -> int:
    users = store.list_users()
    for u in users:
        banned = " BANNED" if u.banned else ""
        print(
            f"{u.login:<30} id={u.github_id:<12} repos={store.owned_count(u.github_id):<3} "
            f"seen {_when(u.last_seen_at)}{banned}"
        )
    print(f"({len(users)} users)")
    return 0


def _reconcile(args, store: AppStore) -> int:
    with Neo4jClient.from_env() as client:
        # The server may be running an ingest right now; leave those rows be.
        print(reconcile(store, client, fail_indexing=False))
    return 0


def _delete(args, store: AppStore) -> int:
    record = store.get_repo(args.repo_id)
    repo_id = record.repo_id if record else args.repo_id
    with Neo4jClient.from_env() as client:
        print(physical_delete(client, repo_id, store))
    return 0


def _public(args, store: AppStore) -> int:
    if not store.set_public(args.repo_id, not args.off):
        print(f"No repository {args.repo_id!r} in the store.")
        return 1
    print(f"{args.repo_id} is now {'private' if args.off else 'public'}.")
    return 0


def _ban(args, store: AppStore, banned: bool = True) -> int:
    user = store.set_banned(args.login, banned)
    if user is None:
        print(f"No user {args.login!r}.")
        return 1
    print(f"{user.login} {'banned; sessions ended' if banned else 'unbanned'}.")
    return 0


_HANDLERS = {
    "repos": _repos,
    "users": _users,
    "reconcile": _reconcile,
    "delete": _delete,
    "public": _public,
    "ban": _ban,
    "unban": lambda args, store: _ban(args, store, banned=False),
}
