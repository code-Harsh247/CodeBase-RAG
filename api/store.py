"""The application's own state: users, sessions and who owns which repository.

SQLite through the standard library. The server is a single process, so one
connection guarded by a lock is enough, and WAL mode lets the admin CLI read
while the server writes. Every method is one short ``BEGIN IMMEDIATE``
transaction, so check-then-act sequences (count owners, then delete) cannot
interleave.

Session tokens are stored only as SHA-256 hashes: a copy of this file does not
let anyone sign in as anybody.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: Each entry upgrades the schema by one version; ``PRAGMA user_version``
#: records how many have run. Append only: never edit a shipped migration.
_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE users (
        github_id     INTEGER PRIMARY KEY,
        login         TEXT NOT NULL,
        avatar_url    TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL,
        last_seen_at  REAL NOT NULL,
        banned        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX users_login ON users (login COLLATE NOCASE);

    CREATE TABLE sessions (
        token_hash  TEXT PRIMARY KEY,
        github_id   INTEGER NOT NULL REFERENCES users (github_id) ON DELETE CASCADE,
        created_at  REAL NOT NULL,
        expires_at  REAL NOT NULL
    );
    CREATE INDEX sessions_user ON sessions (github_id);

    CREATE TABLE repos (
        repo_id       TEXT PRIMARY KEY COLLATE NOCASE,
        public        INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL CHECK (status IN ('indexing', 'ready', 'failed')),
        commit_sha    TEXT,
        files         INTEGER NOT NULL DEFAULT 0,
        nodes         INTEGER NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL,
        last_used_at  REAL NOT NULL
    );

    CREATE TABLE repo_owners (
        github_id  INTEGER NOT NULL REFERENCES users (github_id) ON DELETE CASCADE,
        repo_id    TEXT NOT NULL COLLATE NOCASE REFERENCES repos (repo_id) ON DELETE CASCADE,
        added_at   REAL NOT NULL,
        PRIMARY KEY (github_id, repo_id)
    );
    CREATE INDEX repo_owners_repo ON repo_owners (repo_id);
    """,
    # 2: the spend ledger behind the daily LLM budget (agent/metering.py).
    """
    CREATE TABLE spend (
        day     TEXT PRIMARY KEY,
        usd     REAL NOT NULL DEFAULT 0,
        calls   INTEGER NOT NULL DEFAULT 0,
        tokens  INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE spend_by_user (
        day        TEXT NOT NULL,
        github_id  INTEGER NOT NULL,
        usd        REAL NOT NULL DEFAULT 0,
        calls      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, github_id)
    );
    """,
)

#: last_seen_at is a coarse signal; don't write on every request.
_TOUCH_INTERVAL = 300


@dataclass(frozen=True)
class User:
    github_id: int
    login: str
    avatar_url: str
    banned: bool
    created_at: float
    last_seen_at: float


@dataclass(frozen=True)
class RepoRecord:
    repo_id: str
    public: bool
    status: str
    commit_sha: str | None
    files: int
    nodes: int
    created_at: float
    last_used_at: float
    #: Filled by queries that are about one user; False otherwise.
    owned: bool = False
    owner_count: int = 0


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        github_id=row["github_id"],
        login=row["login"],
        avatar_url=row["avatar_url"],
        banned=bool(row["banned"]),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


def _repo(row: sqlite3.Row | None) -> RepoRecord | None:
    if row is None:
        return None
    keys = row.keys()
    return RepoRecord(
        repo_id=row["repo_id"],
        public=bool(row["public"]),
        status=row["status"],
        commit_sha=row["commit_sha"],
        files=row["files"],
        nodes=row["nodes"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        owned=bool(row["owned"]) if "owned" in keys else False,
        owner_count=row["owner_count"] if "owner_count" in keys else 0,
    )


class AppStore:
    def __init__(self, path: str | Path, clock=time.time) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        # isolation_level=None: no implicit transactions; _tx() opens them.
        self._db = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=10
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 10000")
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def migrate(self) -> int:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(_MIGRATIONS[version:], start=version + 1):
                # executescript commits on its own, so the version bump goes in
                # the same script to keep the two atomic.
                self._db.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )
            return len(_MIGRATIONS)

    # ------------------------------------------------------------------ users

    def upsert_user(self, github_id: int, login: str, avatar_url: str = "") -> User:
        now = self._clock()
        with self._tx() as db:
            db.execute(
                "INSERT INTO users (github_id, login, avatar_url, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (github_id) DO UPDATE SET "
                "login = excluded.login, avatar_url = excluded.avatar_url, "
                "last_seen_at = excluded.last_seen_at",
                (github_id, login, avatar_url, now, now),
            )
            row = db.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
        return _user(row)

    def get_user(self, github_id: int) -> User | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE github_id = ?", (github_id,)
            ).fetchone()
        return _user(row)

    def get_user_by_login(self, login: str) -> User | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE login = ? COLLATE NOCASE", (login,)
            ).fetchone()
        return _user(row)

    def list_users(self) -> list[User]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM users ORDER BY last_seen_at DESC").fetchall()
        return [_user(row) for row in rows]

    def touch_user(self, github_id: int) -> None:
        now = self._clock()
        with self._tx() as db:
            db.execute(
                "UPDATE users SET last_seen_at = ? WHERE github_id = ? AND last_seen_at < ?",
                (now, github_id, now - _TOUCH_INTERVAL),
            )

    def set_banned(self, login: str, banned: bool) -> User | None:
        """Ban or unban by login. A ban also ends every session the user has."""
        with self._tx() as db:
            row = db.execute(
                "SELECT github_id FROM users WHERE login = ? COLLATE NOCASE", (login,)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE users SET banned = ? WHERE github_id = ?", (int(banned), row[0])
            )
            if banned:
                db.execute("DELETE FROM sessions WHERE github_id = ?", (row[0],))
        return self.get_user(row[0])

    # --------------------------------------------------------------- sessions

    def create_session(self, github_id: int, ttl_days: int) -> str:
        """A new session; returns the raw token, which is never stored."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._tx() as db:
            db.execute(
                "INSERT INTO sessions (token_hash, github_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (hash_token(token), github_id, now, now + ttl_days * 86_400),
            )
        return token

    def user_for_session(self, token: str) -> User | None:
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT users.* FROM sessions JOIN users USING (github_id) "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
                (hash_token(token), self._clock()),
            ).fetchone()
        return _user(row)

    def delete_session(self, token: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))

    def purge_expired_sessions(self) -> int:
        with self._tx() as db:
            return db.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (self._clock(),)
            ).rowcount

    # ------------------------------------------------------------------ repos

    def get_repo(self, repo_id: str) -> RepoRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT repos.*, "
                "(SELECT count(*) FROM repo_owners o WHERE o.repo_id = repos.repo_id) "
                "AS owner_count FROM repos WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return _repo(row)

    def list_repos(self) -> list[RepoRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT repos.*, "
                "(SELECT count(*) FROM repo_owners o WHERE o.repo_id = repos.repo_id) "
                "AS owner_count FROM repos ORDER BY repo_id"
            ).fetchall()
        return [_repo(row) for row in rows]

    def visible_repos(self, github_id: int) -> list[RepoRecord]:
        """Ready repositories this user may see: public ones and their own."""
        with self._lock:
            rows = self._db.execute(
                "SELECT repos.*, "
                "EXISTS (SELECT 1 FROM repo_owners o WHERE o.repo_id = repos.repo_id "
                "        AND o.github_id = ?) AS owned "
                "FROM repos WHERE status = 'ready' AND (public = 1 OR EXISTS ("
                "  SELECT 1 FROM repo_owners o WHERE o.repo_id = repos.repo_id "
                "  AND o.github_id = ?)) "
                "ORDER BY repo_id",
                (github_id, github_id),
            ).fetchall()
        return [_repo(row) for row in rows]

    def can_access(self, github_id: int, repo_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM repos WHERE repo_id = ? AND status = 'ready' AND ("
                "public = 1 OR EXISTS (SELECT 1 FROM repo_owners o "
                "WHERE o.repo_id = repos.repo_id AND o.github_id = ?))",
                (repo_id, github_id),
            ).fetchone()
        return row is not None

    def begin_indexing(self, repo_id: str) -> bool:
        """Claim ``repo_id`` for indexing. False if someone is already indexing it."""
        now = self._clock()
        with self._tx() as db:
            row = db.execute("SELECT status FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
            if row is not None and row["status"] == "indexing":
                return False
            if row is None:
                db.execute(
                    "INSERT INTO repos (repo_id, status, created_at, last_used_at) "
                    "VALUES (?, 'indexing', ?, ?)",
                    (repo_id, now, now),
                )
            else:
                db.execute(
                    "UPDATE repos SET status = 'indexing' WHERE repo_id = ?", (repo_id,)
                )
        return True

    def mark_ready(
        self,
        repo_id: str,
        *,
        commit_sha: str | None = None,
        files: int = 0,
        nodes: int = 0,
        public: bool | None = None,
    ) -> None:
        now = self._clock()
        with self._tx() as db:
            db.execute(
                "INSERT INTO repos (repo_id, status, commit_sha, files, nodes, public, "
                "created_at, last_used_at) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (repo_id) DO UPDATE SET status = 'ready', "
                "commit_sha = excluded.commit_sha, files = excluded.files, "
                "nodes = excluded.nodes, last_used_at = excluded.last_used_at",
                (repo_id, commit_sha, files, nodes, int(bool(public)), now, now),
            )
            if public is not None:
                db.execute(
                    "UPDATE repos SET public = ? WHERE repo_id = ?", (int(public), repo_id)
                )

    def mark_failed(self, repo_id: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE repos SET status = 'failed' WHERE repo_id = ?", (repo_id,))

    def touch_repo(self, repo_id: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE repos SET last_used_at = ? WHERE repo_id = ?", (self._clock(), repo_id)
            )

    def set_public(self, repo_id: str, public: bool) -> bool:
        with self._tx() as db:
            return (
                db.execute(
                    "UPDATE repos SET public = ? WHERE repo_id = ?", (int(public), repo_id)
                ).rowcount
                > 0
            )

    def apply_public(self, repo_ids: Iterable[str]) -> None:
        """Make exactly ``repo_ids`` public (the ``PUBLIC_REPOS`` setting)."""
        wanted = {repo_id.lower() for repo_id in repo_ids}
        with self._tx() as db:
            for row in db.execute("SELECT repo_id, public FROM repos").fetchall():
                public = row["repo_id"].lower() in wanted
                if bool(row["public"]) != public:
                    db.execute(
                        "UPDATE repos SET public = ? WHERE repo_id = ?",
                        (int(public), row["repo_id"]),
                    )

    def forget_repo(self, repo_id: str) -> None:
        """Drop the record and every ownership claim (after a physical delete)."""
        with self._tx() as db:
            db.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))

    # ------------------------------------------------------------- ownership

    def add_owner(self, github_id: int, repo_id: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT OR IGNORE INTO repo_owners (github_id, repo_id, added_at) "
                "VALUES (?, ?, ?)",
                (github_id, repo_id, self._clock()),
            )

    def is_owner(self, github_id: int, repo_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM repo_owners WHERE github_id = ? AND repo_id = ?",
                (github_id, repo_id),
            ).fetchone()
        return row is not None

    def remove_owner(self, github_id: int, repo_id: str) -> int:
        """Drop one claim; returns how many owners remain."""
        with self._tx() as db:
            db.execute(
                "DELETE FROM repo_owners WHERE github_id = ? AND repo_id = ?",
                (github_id, repo_id),
            )
            return db.execute(
                "SELECT count(*) FROM repo_owners WHERE repo_id = ?", (repo_id,)
            ).fetchone()[0]

    def owned_count(self, github_id: int) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT count(*) FROM repo_owners WHERE github_id = ?", (github_id,)
            ).fetchone()[0]

    # ----------------------------------------------------------------- spend

    def add_spend(self, day: str, github_id: int, usd: float, tokens: int = 0) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO spend (day, usd, calls, tokens) VALUES (?, ?, 1, ?) "
                "ON CONFLICT (day) DO UPDATE SET usd = usd + excluded.usd, "
                "calls = calls + 1, tokens = tokens + excluded.tokens",
                (day, usd, tokens),
            )
            db.execute(
                "INSERT INTO spend_by_user (day, github_id, usd, calls) VALUES (?, ?, ?, 1) "
                "ON CONFLICT (day, github_id) DO UPDATE SET usd = usd + excluded.usd, "
                "calls = calls + 1",
                (day, github_id, usd),
            )

    def day_spend(self, day: str) -> float:
        with self._lock:
            row = self._db.execute("SELECT usd FROM spend WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0.0

    def user_spend(self, day: str, github_id: int) -> float:
        with self._lock:
            row = self._db.execute(
                "SELECT usd FROM spend_by_user WHERE day = ? AND github_id = ?", (day, github_id)
            ).fetchone()
        return row[0] if row else 0.0

    def spend_history(self, days: int = 14) -> list[dict]:
        """Most recent days first, each with its top spenders."""
        with self._lock:
            rows = self._db.execute(
                "SELECT day, usd, calls, tokens FROM spend ORDER BY day DESC LIMIT ?", (days,)
            ).fetchall()
            history = []
            for row in rows:
                top = self._db.execute(
                    "SELECT coalesce(users.login, spend_by_user.github_id) AS who, "
                    "spend_by_user.usd, spend_by_user.calls FROM spend_by_user "
                    "LEFT JOIN users USING (github_id) WHERE day = ? "
                    "ORDER BY spend_by_user.usd DESC LIMIT 5",
                    (row["day"],),
                ).fetchall()
                history.append(
                    {
                        "day": row["day"],
                        "usd": row["usd"],
                        "calls": row["calls"],
                        "tokens": row["tokens"],
                        "top": [(str(t["who"]), t["usd"], t["calls"]) for t in top],
                    }
                )
        return history

    # ------------------------------------------------------------- reconcile

    def reconcile(
        self, graph_repos: dict[str, int], *, fail_indexing: bool = True
    ) -> dict[str, int]:
        """Bring the records in line with what the graph actually holds.

        ``graph_repos`` maps each repo id present in Neo4j to its node count.
        Repos indexed before this store existed are adopted as ready and
        ownerless (admins can make them public). ``ready`` rows whose graph has
        gone are marked failed, and so are rows left ``indexing`` by a crash —
        but only when ``fail_indexing`` is set, i.e. at server startup, when no
        ingest can be running.
        """
        now = self._clock()
        present = {repo_id.lower(): (repo_id, nodes) for repo_id, nodes in graph_repos.items()}
        adopted = failed = 0
        with self._tx() as db:
            known = {
                row["repo_id"].lower(): row
                for row in db.execute("SELECT repo_id, status FROM repos").fetchall()
            }
            for key, (repo_id, nodes) in present.items():
                if key not in known:
                    db.execute(
                        "INSERT INTO repos (repo_id, status, nodes, created_at, last_used_at) "
                        "VALUES (?, 'ready', ?, ?, ?)",
                        (repo_id, nodes, now, now),
                    )
                    adopted += 1
            for key, row in known.items():
                stuck = fail_indexing and row["status"] == "indexing"
                vanished = row["status"] == "ready" and key not in present
                if stuck or vanished:
                    db.execute(
                        "UPDATE repos SET status = 'failed' WHERE repo_id = ?", (row["repo_id"],)
                    )
                    failed += 1
        return {"adopted": adopted, "failed": failed}
