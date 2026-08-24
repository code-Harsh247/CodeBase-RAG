import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchRepos,
  streamQuery,
  type AnswerEvent,
  type HopEvent,
  type Mode,
  type Repo,
} from "./api";
import { AddRepo } from "./components/AddRepo";
import { Answer } from "./components/Answer";
import { HopTrace } from "./components/HopTrace";
import { examplesFor } from "./examples";

export default function App() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [repoId, setRepoId] = useState("");
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<Mode>("multi_hop");
  const [adding, setAdding] = useState(false);

  const [hops, setHops] = useState<HopEvent[]>([]);
  const [answer, setAnswer] = useState<AnswerEvent | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const loadRepos = useCallback(async (select?: string) => {
    const found = await fetchRepos();
    setRepos(found);
    setRepoId((current) => select ?? current ?? "");
    return found;
  }, []);

  useEffect(() => {
    loadRepos()
      .then((found) => {
        if (found.length) setRepoId((current) => current || found[0].repo_id);
        // Nothing indexed yet: the URL box is the only sensible starting point.
        else setAdding(true);
      })
      .catch((exc) => setError(String(exc)));
  }, [loadRepos]);

  // A run can take half a minute; leaving it going after the component
  // unmounts would leak the connection.
  useEffect(() => () => abortRef.current?.abort(), []);

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || !repoId || running) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setRunning(true);
    setHops([]);
    setAnswer(null);
    setError("");

    try {
      await streamQuery(
        { repo_id: repoId, question: trimmed, mode },
        (event) => {
          if (event.type === "hop") setHops((current) => [...current, event]);
          else if (event.type === "answer") setAnswer(event);
          else setError(event.message);
        },
        controller.signal,
      );
    } catch (exc) {
      if (!controller.signal.aborted) setError(String(exc));
    } finally {
      setRunning(false);
    }
  }

  function onIngested(newRepoId: string) {
    void loadRepos(newRepoId).then(() => {
      setAdding(false);
      setHops([]);
      setAnswer(null);
    });
  }

  const selected = repos.find((repo) => repo.repo_id === repoId);
  const examples = examplesFor(repoId);
  const hasRepos = repos.length > 0;

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>CodeGraph</h1>
          <p className="tagline">
            Index a Python repository, then ask it questions. Every answer is
            traced back to the code it came from.
          </p>
        </div>
        {hasRepos && (
          <div className="controls">
            <label>
              <span>Repository</span>
              <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
                {repos.map((repo) => (
                  <option key={repo.repo_id} value={repo.repo_id}>
                    {repo.repo_id} ({repo.nodes.toLocaleString()} nodes)
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Retrieval</span>
              <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                <option value="multi_hop">Multi-hop agent</option>
                <option value="single_hop">Single query</option>
              </select>
            </label>
          </div>
        )}
      </header>

      {hasRepos && (
        <button
          type="button"
          className="toggle-add"
          onClick={() => setAdding((current) => !current)}
        >
          {adding ? "− Cancel" : "+ Index another repository"}
        </button>
      )}

      {(adding || !hasRepos) && <AddRepo onIngested={onIngested} />}

      {hasRepos && (
        <>
          {selected && !selected.has_source && (
            <p className="notice">
              No local checkout for {selected.repo_id} — reading source and grep
              are unavailable, so the agent can only use the graph and semantic
              search.
            </p>
          )}

          <form
            className="ask"
            onSubmit={(e) => {
              e.preventDefault();
              void ask(question);
            }}
          >
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="e.g. Where is SSL certificate verification handled?"
              disabled={running || !repoId}
            />
            <button type="submit" disabled={running || !question.trim() || !repoId}>
              {running ? "Investigating…" : "Ask"}
            </button>
          </form>

          {!hops.length && !answer && !running && (
            <div className="examples">
              {examples.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => {
                    setQuestion(example);
                    void ask(example);
                  }}
                  disabled={!repoId}
                >
                  {example}
                </button>
              ))}
            </div>
          )}
        </>
      )}

      {error && <p className="error">{error}</p>}

      <HopTrace hops={hops} running={running} mode={mode} />
      {answer && <Answer answer={answer} />}
    </div>
  );
}
