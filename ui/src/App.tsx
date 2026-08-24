import { useCallback, useEffect, useMemo, useState } from "react";
import { deleteRepo, fetchRepos, type Repo } from "./api";
import { Composer } from "./components/Composer";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { Sidebar, type SidebarRow } from "./components/Sidebar";
import { Thread } from "./components/Thread";
import { TracePanel } from "./components/TracePanel";
import { newId } from "./state/types";
import { useThreadStore } from "./state/useThreadStore";

export default function App() {
  const { store, dispatch, startIngest, ask } = useThreadStore();
  const [repos, setRepos] = useState<Repo[]>([]);
  const [reposError, setReposError] = useState("");
  const [traceMessageId, setTraceMessageId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<SidebarRow | null>(null);

  const refreshRepos = useCallback(async () => {
    try {
      setRepos(await fetchRepos());
      setReposError("");
    } catch (exc) {
      // A failed fetch must never be read as "everything is stale" — keep the
      // local threads and say the server is unreachable.
      setReposError(String(exc));
    }
  }, []);

  useEffect(() => {
    void refreshRepos();
  }, [refreshRepos]);

  const activeThread = store.activeThreadId
    ? (store.threads[store.activeThreadId] ?? null)
    : null;

  const running = activeThread
    ? activeThread.messages.some(
        (message) => message.kind !== "user" && message.status === "running",
      )
    : false;

  // One row per project: local threads first, then server repos never opened
  // here. A thread whose repo has vanished from the graph is kept and marked,
  // never dropped — the transcript is still worth reading.
  const rows: SidebarRow[] = useMemo(() => {
    const indexed = new Map(repos.map((repo) => [repo.repo_id, repo]));
    const threads = Object.values(store.threads).sort((a, b) => b.updatedAt - a.updatedAt);
    const claimed = new Set<string>();

    // A thread that never produced anything — a failed ingest — should not
    // leave a permanent row behind. `start` assigns a repoId before the clone
    // is attempted, so a nonexistent repo does get an id; what distinguishes a
    // real project is a completed ingest or a repo the server actually has.
    const survives = (thread: (typeof threads)[number]) =>
      thread.id === store.activeThreadId ||
      thread.messages.some(
        (message) => message.kind === "ingest" && message.status === "done",
      ) ||
      (!!thread.repoId && indexed.has(thread.repoId));

    // Re-indexing a repository creates a second thread for it. Show only the
    // most recent — `threads` is already sorted by updatedAt, so the first one
    // seen for a repoId wins and later duplicates are dropped.
    const seenRepo = new Set<string>();
    const deduped = threads.filter(survives).filter((thread) => {
      if (!thread.repoId) return true;
      if (seenRepo.has(thread.repoId)) return false;
      seenRepo.add(thread.repoId);
      return true;
    });

    const fromThreads = deduped.map((thread) => {
      if (thread.repoId) claimed.add(thread.repoId);
      const repo = thread.repoId ? indexed.get(thread.repoId) : undefined;
      return {
        thread,
        repoId: thread.repoId,
        label: thread.repoId ?? thread.url ?? "New project",
        nodes: repo?.nodes ?? null,
        // With no server list we cannot know; assume available rather than
        // marking every thread stale because Neo4j is down.
        available: reposError ? true : !thread.repoId || indexed.has(thread.repoId),
        running: thread.messages.some(
          (message) => message.kind !== "user" && message.status === "running",
        ),
      };
    });

    const fromServer = repos
      .filter((repo) => !claimed.has(repo.repo_id))
      .map((repo) => ({
        thread: null,
        repoId: repo.repo_id,
        label: repo.repo_id,
        nodes: repo.nodes,
        available: true,
        running: false,
      }));

    return [...fromThreads, ...fromServer];
  }, [repos, store.threads, store.activeThreadId, reposError]);

  function selectRow(row: SidebarRow) {
    setTraceMessageId(null);
    if (row.thread) {
      dispatch({ type: "activeThreadSet", threadId: row.thread.id });
      return;
    }
    // A repo indexed on the server that this browser has never opened: adopt
    // it into an empty thread so it can be asked about.
    if (row.repoId) {
      dispatch({ type: "threadAdopted", threadId: newId(), repoId: row.repoId });
    }
  }

  /** Runs only after the dialog is confirmed. */
  async function removeProject(row: SidebarRow) {
    setPendingDelete(null);

    // Re-indexing a repository leaves more than one thread for it, and the
    // sidebar only shows the newest. Deleting the project has to take all of
    // its history, or the older ones resurface once the visible row is gone.
    const doomed = new Set<string>();
    if (row.thread) doomed.add(row.thread.id);
    if (row.repoId) {
      for (const thread of Object.values(store.threads)) {
        if (thread.repoId === row.repoId) doomed.add(thread.id);
      }
    }
    for (const threadId of doomed) {
      dispatch({ type: "threadRemoved", threadId });
    }

    if (!row.repoId) return;
    setDeleting(row.repoId);
    try {
      await deleteRepo(row.repoId);
    } catch (exc) {
      setReposError(String(exc));
    } finally {
      setDeleting(null);
      await refreshRepos();
    }
  }

  async function onSubmit(text: string) {
    setTraceMessageId(null);
    if (!activeThread || !activeThread.repoId) {
      await startIngest(text);
      await refreshRepos();
      return;
    }
    await ask(activeThread, text);
  }

  const activeRepo = activeThread?.repoId
    ? repos.find((repo) => repo.repo_id === activeThread.repoId)
    : undefined;

  const tracedMessage = activeThread?.messages.find(
    (message) => message.id === traceMessageId && message.kind === "assistant",
  );
  const tracedHops =
    tracedMessage && tracedMessage.kind === "assistant" ? tracedMessage.hops : null;

  const showHero = !activeThread;
  const doomedLabel = pendingDelete?.label ?? "";

  return (
    <div className="shell">
      <Sidebar
        rows={rows}
        activeThreadId={store.activeThreadId}
        activeRepoId={activeThread?.repoId ?? null}
        reposError={reposError}
        deleting={deleting}
        onSelect={selectRow}
        onDelete={setPendingDelete}
        onNew={() => {
          setTraceMessageId(null);
          dispatch({ type: "activeThreadSet", threadId: null });
        }}
      />

      <main className="main">
        {showHero ? (
          <div className="hero">
            <h1>CodeGraph</h1>
            <p className="tagline">
              Index a Python repository, then ask it questions. Every answer is
              traced back to the code it came from.
            </p>
          </div>
        ) : (
          <Thread
            thread={activeThread}
            nodes={activeRepo?.nodes ?? null}
            hasSource={activeRepo?.has_source ?? true}
            onAsk={(text) => void onSubmit(text)}
            onOpenTrace={setTraceMessageId}
          />
        )}

        <div className={`composer-dock${showHero ? " composer-dock--hero" : ""}`}>
          <Composer
            variant={showHero ? "hero" : "docked"}
            intent={activeThread?.repoId ? "question" : "url"}
            disabled={running}
            onSubmit={(text) => void onSubmit(text)}
          />
        </div>
      </main>

      <TracePanel
        hops={tracedHops}
        running={running}
        onClose={() => setTraceMessageId(null)}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`Delete ${doomedLabel}?`}
        body={`This removes its graph, embeddings, cloned source and chat history. Re-adding it means indexing again.`}
        onConfirm={() => pendingDelete && void removeProject(pendingDelete)}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
