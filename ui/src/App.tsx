import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchRepos, type Repo } from "./api";
import { Composer } from "./components/Composer";
import { Sidebar, type SidebarRow } from "./components/Sidebar";
import { Thread } from "./components/Thread";
import { TracePanel } from "./components/TracePanel";
import { loadSidebarOpen, saveSidebarOpen } from "./state/storage";
import { newId } from "./state/types";
import { useThreadStore } from "./state/useThreadStore";

export default function App() {
  const { store, dispatch, startIngest, ask } = useThreadStore();
  const [repos, setRepos] = useState<Repo[]>([]);
  const [reposError, setReposError] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(loadSidebarOpen);
  const [traceMessageId, setTraceMessageId] = useState<string | null>(null);

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

  useEffect(() => saveSidebarOpen(sidebarOpen), [sidebarOpen]);

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

    const fromThreads = threads.filter(survives).map((thread) => {
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

  return (
    <div className={`shell${sidebarOpen ? "" : " collapsed"}`}>
      <Sidebar
        rows={rows}
        activeThreadId={store.activeThreadId}
        activeRepoId={activeThread?.repoId ?? null}
        open={sidebarOpen}
        reposError={reposError}
        onToggle={() => setSidebarOpen((open) => !open)}
        onSelect={selectRow}
        onNew={() => {
          setTraceMessageId(null);
          dispatch({ type: "activeThreadSet", threadId: null });
        }}
      />

      <main className="main">
        {!sidebarOpen && (
          <button
            type="button"
            className="icon-button expand"
            onClick={() => setSidebarOpen(true)}
            aria-label="Expand sidebar"
          >
            ›
          </button>
        )}

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
    </div>
  );
}
