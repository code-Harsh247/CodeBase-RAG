"""Worked question -> Cypher examples.

These carry the conventions that the schema description alone does not teach:
matching on `name` versus `qualified_name`, the two-hop shape of IMPORTS, using
DEFINES for methods, and returning citation fields.
"""

from __future__ import annotations

EXAMPLES: list[tuple[str, str]] = [
    (
        "What calls the function `send`?",
        """
MATCH (caller)-[:CALLS]->(target {repo_id: $repo_id})
WHERE target.name = 'send'
RETURN caller.qualified_name AS caller, caller.file_path AS file,
       caller.start_line AS line
ORDER BY caller LIMIT 25
""".strip(),
    ),
    (
        "What does requests.sessions.Session.get call?",
        """
MATCH (:Method {repo_id: $repo_id, qualified_name: 'requests.sessions.Session.get'})
      -[:CALLS]->(callee)
RETURN callee.qualified_name AS callee, callee.file_path AS file,
       callee.start_line AS line
ORDER BY callee LIMIT 25
""".strip(),
    ),
    (
        "Which classes inherit from RequestException?",
        """
MATCH (sub:Class {repo_id: $repo_id})-[:INHERITS]->(base:Class)
WHERE base.name = 'RequestException'
RETURN sub.qualified_name AS subclass, sub.file_path AS file,
       sub.start_line AS line
ORDER BY subclass LIMIT 25
""".strip(),
    ),
    (
        "What methods does the Session class define?",
        """
MATCH (c:Class {repo_id: $repo_id})-[:DEFINES]->(m:Method)
WHERE c.name = 'Session'
RETURN m.name AS method, m.signature AS signature, m.file_path AS file,
       m.start_line AS line
ORDER BY m.start_line LIMIT 25
""".strip(),
    ),
    (
        "Which modules import requests.models?",
        """
MATCH (m:Module {repo_id: $repo_id})-[:IMPORTS]->(i:Import)-[:IMPORTS]->(target:Module)
WHERE target.qualified_name = 'requests.models'
RETURN DISTINCT m.qualified_name AS importer, collect(DISTINCT i.name) AS names
ORDER BY importer LIMIT 25
""".strip(),
    ),
    (
        "What is the class hierarchy above ConnectionError?",
        """
MATCH path = (c:Class {repo_id: $repo_id})-[:INHERITS*1..5]->(base:Class)
WHERE c.name = 'ConnectionError'
RETURN base.qualified_name AS ancestor, length(path) AS depth,
       base.file_path AS file
ORDER BY depth LIMIT 25
""".strip(),
    ),
    (
        "Which functions are never called anywhere in the repo?",
        """
MATCH (f:Function {repo_id: $repo_id})
WHERE NOT ()-[:CALLS]->(f)
RETURN f.qualified_name AS uncalled, f.file_path AS file,
       f.start_line AS line
ORDER BY uncalled LIMIT 25
""".strip(),
    ),
    (
        "What are the most-called functions?",
        """
MATCH (target {repo_id: $repo_id})<-[c:CALLS]-()
RETURN target.qualified_name AS target, count(c) AS callers,
       target.file_path AS file
ORDER BY callers DESC, target LIMIT 10
""".strip(),
    ),
    (
        "What classes does prepare_request use?",
        """
MATCH (m {repo_id: $repo_id})-[:REFERENCES]->(c:Class)
WHERE m.name = 'prepare_request'
RETURN DISTINCT c.qualified_name AS referenced, c.file_path AS file,
       c.start_line AS line
ORDER BY referenced LIMIT 25
""".strip(),
    ),
    (
        "What is defined in the file src/requests/api.py?",
        """
MATCH (f:File {repo_id: $repo_id})-[:CONTAINS]->(:Module)-[:CONTAINS]->(d)
WHERE f.qualified_name = 'src/requests/api.py'
RETURN [l IN labels(d) WHERE l <> 'CodeNode'][0] AS kind, d.name AS name,
       d.start_line AS line
ORDER BY d.start_line LIMIT 25
""".strip(),
    ),
    (
        "Which functions call json.dumps?",
        """
MATCH (caller:Function|Method {repo_id: $repo_id})-[:CALLS]->(target)
WHERE target.name = 'dumps'
RETURN caller.qualified_name AS caller, caller.file_path AS file,
       caller.start_line AS line
ORDER BY caller LIMIT 25
""".strip(),
    ),
]


def render_examples(limit: int | None = None) -> str:
    """Render examples for a prompt.

    ``limit`` matters for the multi-hop agent: its system prompt is re-sent on
    every hop, so each example is paid for repeatedly. The first few cover the
    patterns that generalise (callers, callees, inheritance, methods); the rest
    earn their place only in the single-shot path, which pays for them once.
    """
    chosen = EXAMPLES[:limit] if limit else EXAMPLES
    blocks = ["EXAMPLES", ""]
    for question, cypher in chosen:
        blocks.append(f"Q: {question}")
        blocks.append(f"Cypher:\n{cypher}")
        blocks.append("")
    return "\n".join(blocks)
