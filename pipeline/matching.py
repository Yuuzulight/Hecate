"""Matching a post to a project by name rather than by link.

URL matching is reliable: a story linking github.com/owner/repo is about that
repository and nothing else. Name matching is not, and the failure is quiet -
it attaches attention to the wrong project and the result looks perfectly
plausible in a ranking.

The collisions are real. `requests` is a PyPI package, a GitHub repository and
an ordinary English word. So are `next`, `rust`, `core`, `data`, `test`. A
naive substring match turns every post containing the word "test" into a
mention of a project called test.

So this is deliberately conservative, off by default, and its error rate is
measured against the URL-resolved set rather than estimated. See
tools/measure_name_matching.py.
"""

import re

# - Below this, a name is too generic to carry meaning on its own. Rules out
#   go, id, ai, ml and npm in one stroke.
#
#   Four rather than five, because four-letter project names are common and
#   real - axum, tokio, curl - and excluding them would leave the feature
#   unable to match the example that motivated it. The cost is that more
#   ordinary words clear the bar, which the stop list below absorbs.
MIN_LENGTH = 4

# - Names that clear the length rule and are still ordinary English, or are
#   generic enough in a technical post to mean nothing. Matching any of these
#   is almost always a false positive.
STOP_NAMES = frozenset({
    # - Four letters, admitted by the length rule and meaningless in prose.
    "apis", "apps", "beta", "blog", "body", "book", "chat", "code", "demo",
    "docs", "file", "flow", "font", "form", "free", "full", "game", "grid",
    "help", "home", "hook", "html", "http", "icon", "item", "java", "json",
    "keys", "link", "list", "live", "load", "lock", "logs", "main", "make",
    "menu", "meta", "mode", "name", "news", "next", "node", "note", "open",
    "page", "path", "port", "post", "read", "repo", "rest", "role", "root",
    "rust", "save", "send", "shop", "show", "site", "size", "sort", "spec",
    "sync", "tags", "task", "team", "temp", "test", "text", "time", "todo",
    "tool", "type", "unit", "user", "view", "wiki", "work", "yaml", "zero",

    "about", "action", "actions", "agent", "agents", "alpha", "angular",
    "assets", "async", "audio", "auth", "awesome", "block", "blog", "build",
    "cache", "chart", "class", "client", "cloud", "color", "components",
    "config", "console", "content", "context", "core", "data", "database",
    "debug", "deploy", "design", "docker", "docs", "editor", "email", "engine",
    "error", "event", "events", "example", "examples", "field", "files",
    "focus", "forms", "frame", "games", "graph", "group", "guide", "hooks",
    "image", "images", "index", "input", "issue", "issues", "learn", "level",
    "library", "light", "linux", "lists", "login", "media", "memory", "model",
    "models", "modern", "module", "music", "native", "network", "notes",
    "order", "pages", "paper", "parser", "photo", "plugin", "point", "posts",
    "power", "print", "project", "proxy", "query", "queue", "react", "reader",
    "release", "remote", "render", "report", "requests", "research", "router",
    "rules", "scale", "scene", "schema", "script", "search", "secret",
    "series", "server", "shell", "simple", "space", "stack", "start", "state",
    "static", "stats", "storage", "store", "stream", "style", "swift", "sync",
    "table", "tasks", "template", "terminal", "tests", "theme", "tools",
    "track", "types", "utils", "value", "video", "views", "watch", "world",
    "write",
})


def is_matchable(name: str) -> bool:
    """Whether a project name is distinctive enough to look for in prose."""
    if not name:
        return False
    lowered = name.strip().lower()
    if len(lowered) < MIN_LENGTH:
        return False
    if lowered in STOP_NAMES:
        return False
    # - Purely numeric names carry no signal and collide with version numbers.
    return not lowered.isdigit()


def name_candidates(text: str, names: dict[str, str]) -> set[str]:
    """Repository ids whose name appears in the text as a whole word.

    `names` maps a lowercased project name to its repository id. Returns ids,
    empty when nothing distinctive matched.

    Whole words only: without a boundary check, `axum` matches inside `maxumum`
    and every project whose name is a common substring collects mentions it has
    nothing to do with.
    """
    if not text:
        return set()

    lowered = text.lower()
    found = set()
    for name, repository_id in names.items():
        if not is_matchable(name):
            continue
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", lowered):
            found.add(repository_id)
    return found


def resolve_by_name(text: str, names: dict[str, str]) -> str | None:
    """The single repository this text names, or None.

    Ambiguity is treated as no answer. Two projects named in one post is common
    - a comparison, a list - and guessing between them is exactly the quiet
    wrongness this module exists to avoid.
    """
    candidates = name_candidates(text, names)
    return candidates.pop() if len(candidates) == 1 else None
