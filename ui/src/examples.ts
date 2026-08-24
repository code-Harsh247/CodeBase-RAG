/**
 * Starter questions.
 *
 * Repo-specific ones demo far better than generic ones, but the generic set is
 * the fallback so that ingesting a new repository does not leave the user
 * staring at questions about a codebase they did not load.
 */
const BY_REPO: Record<string, string[]> = {
  "psf/requests": [
    "Where is SSL certificate verification handled?",
    "Which classes inherit directly from RequestException?",
    "How does requests decide whether to follow a redirect?",
    "What calls the function _basic_auth_str?",
  ],
  "pallets/click": [
    "What does the Context class do?",
    "Which classes inherit from Command?",
    "How does click parse command line arguments?",
    "What calls Context.invoke?",
  ],
};

const GENERIC = [
  "What are the most-called functions in this codebase?",
  "Which classes have subclasses, and what are they?",
  "Where is error handling concentrated?",
  "Which functions are never called anywhere?",
];

export function examplesFor(repoId: string): string[] {
  return BY_REPO[repoId] ?? GENERIC;
}
