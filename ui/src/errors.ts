import { ApiError } from "./api";

/**
 * Wording for every error code the server sends, used when a response carries
 * a code but no message (or when a stream's error event is read back from an
 * older save). The server's own message wins when present: it is written for
 * people and often carries specifics — "about 500 MB", "up to 5 projects".
 */
const BY_CODE: Record<string, string> = {
  login_required: "Your session has ended. Sign in again to continue.",
  banned: "This account has been suspended.",
  rate_limited: "Too many requests. Please wait a little and try again.",
  user_busy: "You already have a question running. Wait for it to finish.",
  busy: "The server is answering as many questions as it can. Try again in a moment.",
  ingest_busy: "The server is busy indexing other repositories. Try again in a few minutes.",
  repo_busy: "That repository is being indexed right now. Try again shortly.",
  budget_exhausted: "The demo has used its budget for today. It resets at 00:00 UTC.",
  user_budget_exhausted:
    "You have used your share of today's demo budget. It resets at 00:00 UTC.",
  repo_limit: "You have reached the project limit. Remove a project to add another.",
  repo_too_large: "That repository is too large for this demo.",
  repo_not_found:
    "Repository not found. Check the URL; private repositories are not supported.",
  invalid_url: "That doesn't look like a GitHub repository URL.",
  invalid_repo: "That isn't a valid repository.",
  no_python_files: "That repository has no Python source to index.",
  clone_failed: "Could not download the repository from GitHub. Try again shortly.",
  server_full: "The server is out of space for new repositories right now.",
  timeout: "That took too long and was stopped.",
  cancelled: "The request was cancelled.",
  internal: "Something went wrong on the server. Please try again.",
};

const UNREACHABLE = "The server is restarting or unreachable. Try again in a minute.";

function byStatus(status: number, emptyBody: boolean): string {
  if (status === 429) return BY_CODE.rate_limited;
  // A bare 502/503/504, or any 5xx with no body, came from a proxy (Caddy in
  // production, Vite in development) because the API itself did not answer.
  if ((status >= 502 && status <= 504) || (status >= 500 && emptyBody)) return UNREACHABLE;
  if (status >= 500) return BY_CODE.internal;
  return `The request failed (${status}).`;
}

/**
 * By name as well as class: after a hot reload in development, two copies of
 * the `ApiError` class can exist and `instanceof` alone would miss one.
 */
function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError || (error instanceof Error && error.name === "ApiError");
}

/** One sentence a person can act on, for anything a request can throw. */
export function describeError(error: unknown): string {
  if (isApiError(error)) {
    return (
      error.serverMessage ||
      (error.code && BY_CODE[error.code]) ||
      byStatus(error.status, error.emptyBody)
    );
  }
  // fetch() rejects with a TypeError when the network or server is unreachable.
  if (error instanceof TypeError) {
    return "Could not reach the server. Check your connection and try again.";
  }
  if (error instanceof Error && error.message) return error.message;
  return BY_CODE.internal;
}

/** The text for an `error` event that ended a stream. */
export function describeStreamError(event: { code?: string; message?: string }): string {
  return event.message || (event.code && BY_CODE[event.code]) || BY_CODE.internal;
}
