/**
 * Who is signed in. The session lives in an HttpOnly cookie the page cannot
 * read, so the only way to know is to ask the server.
 */

export interface Me {
  login: string;
  avatar_url: string;
  is_admin: boolean;
}

/** Fired by any API call that gets a 401, so the app drops to the sign-in screen. */
export const UNAUTHORIZED_EVENT = "codegraph:unauthorized";

export const LOGIN_URL = "/api/auth/login";

export function notifyUnauthorized(): void {
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}

/** The current user, or null when signed out. Throws only if the server is unreachable. */
export async function fetchMe(): Promise<Me | null> {
  const response = await fetch("/api/auth/me");
  if (response.status === 401 || response.status === 403) return null;
  if (!response.ok) throw new Error(`Could not reach the server (${response.status})`);
  return (await response.json()) as Me;
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST" });
}

const AUTH_ERRORS: Record<string, string> = {
  state: "Sign-in expired or was started in another tab. Please try again.",
  denied: "GitHub sign-in was cancelled.",
  github: "Could not reach GitHub to sign you in. Please try again.",
  banned: "This account has been suspended.",
  unavailable: "Sign-in is not configured on this server.",
};

let takenAuthError: string | null = null;

/**
 * Read and clear `?auth_error=` left by a failed OAuth callback, so a refresh
 * does not show the same error again.
 *
 * Memoised: StrictMode runs state initialisers twice in development, and the
 * second run would otherwise find the parameter already gone.
 */
export function takeAuthError(): string {
  if (takenAuthError !== null) return takenAuthError;
  const url = new URL(window.location.href);
  const code = url.searchParams.get("auth_error");
  takenAuthError = code ? (AUTH_ERRORS[code] ?? "Sign-in failed. Please try again.") : "";
  if (code) {
    url.searchParams.delete("auth_error");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  return takenAuthError;
}
