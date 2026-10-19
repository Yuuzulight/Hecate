"""Publish a small public snapshot of the warehouse: weekly-growth.svg and summary.json.

Run by the daily batch through aws_runtime.py. Reads only the dbt marts and
three row counts, all of which describe public repositories and packages. Run
history, job errors and mention text never pass through here.
"""

import json
import os
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import boto3
import psycopg2

MARTS = "analytics_marts"
TOP = 8

TOTALS = f"""
SELECT (SELECT count(*) FROM {MARTS}.fct_repositories),
       (SELECT count(DISTINCT captured_on) FROM repository_snapshots),
       (SELECT count(*) FROM social_mentions),
       (SELECT max(as_of) FROM {MARTS}.fct_repository_growth),
       (SELECT coalesce(sum(stars_gained_7d), 0) FROM {MARTS}.fct_repository_growth),
       (SELECT coalesce(sum(stars_gained_30d), 0) FROM {MARTS}.fct_repository_growth)
"""
GAINERS = f"""
SELECT r.name, g.stars_gained_7d FROM {MARTS}.fct_repository_growth g
JOIN {MARTS}.fct_repositories r ON r.id = g.repository_id
WHERE g.stars_gained_7d > 0 ORDER BY g.stars_gained_7d DESC, r.name LIMIT {TOP}
"""
# A project with one signal scores that signal alone, and newly discovered repositories with only a
# capped attention score all tie at 100. Require two signals so the ranking compares like with like.
MOMENTUM = f"""
SELECT r.name, m.momentum FROM {MARTS}.fct_momentum m
JOIN {MARTS}.fct_repositories r ON r.id = m.repository_id
WHERE m.signals_measured >= 2 ORDER BY m.momentum DESC, m.signals_measured DESC, m.stars DESC NULLS LAST, r.name LIMIT {TOP}
"""


def fetch(connection):
    with connection.cursor() as cursor:
        cursor.execute(TOTALS)
        repos, days, mentions, as_of, gained_7d, gained_30d = cursor.fetchone()
        cursor.execute(GAINERS)
        gainers = [(name, int(value)) for name, value in cursor.fetchall()]
        cursor.execute(MOMENTUM)
        momentum = [(name, float(value)) for name, value in cursor.fetchall()]
    return {
        "repositories": repos, "snapshot_days": days, "mentions": mentions,
        "as_of": as_of.isoformat() if as_of else None,
        "stars_gained_7d": int(gained_7d), "stars_gained_30d": int(gained_30d),
        "gainers": gainers, "momentum": momentum,
    }


def summary(data, generated):
    keys = ("repositories", "snapshot_days", "mentions", "stars_gained_7d", "stars_gained_30d", "as_of")
    return {**{key: data[key] for key in keys}, "generated": generated.date().isoformat()}


def rows(items, x, y, width, fmt):
    """Name, value and a proportional bar per item; the first item sets the scale."""
    if not items:
        return f'<text x="{x}" y="{y + 16}" class="mut">Not enough history yet</text>'
    top = max(value for _, value in items) or 1
    out = []
    for i, (name, value) in enumerate(items):
        top_y = y + i * 34
        name = name if len(name) <= 30 else name[:29] + "…"
        out.append(
            f'<text x="{x}" y="{top_y + 12}" class="txt">{escape(name)}</text>'
            f'<text x="{x + width}" y="{top_y + 12}" class="txt" text-anchor="end">{fmt(value)}</text>'
            f'<rect x="{x}" y="{top_y + 18}" width="{width}" height="5" rx="2.5" class="trk"/>'
            f'<rect x="{x}" y="{top_y + 18}" width="{max(width * value / top, 3):.1f}" height="5" rx="2.5" class="bar"/>'
        )
    return "".join(out)


def render_svg(data, generated):
    kpis = [
        (f'+{data["stars_gained_7d"]:,}', "stars gained, 7 days"),
        (f'+{data["stars_gained_30d"]:,}', "stars gained, 30 days"),
        (f'{data["repositories"]:,}', "repositories tracked"),
    ]
    kpi = "".join(
        f'<text x="{32 + i * 240}" y="96" class="big">{escape(v)}</text>'
        f'<text x="{32 + i * 240}" y="116" class="mut">{label}</text>'
        for i, (v, label) in enumerate(kpis)
    )
    footer = (f'{data["snapshot_days"]:,} snapshot days · {data["mentions"]:,} mentions · '
              f'generated {generated.date().isoformat()}')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="760" height="460" viewBox="0 0 760 460" role="img" aria-label="Hecate weekly growth">
<style>
svg{{--bg:#fff;--fg:#1f2328;--mut:#656d76;--trk:#eaeef2;--bar:#6d4aff}}
@media (prefers-color-scheme:dark){{svg{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--trk:#21262d;--bar:#a78bfa}}}}
text{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
.bg{{fill:var(--bg)}}.trk{{fill:var(--trk)}}.bar{{fill:var(--bar)}}
.txt{{fill:var(--fg);font-size:12px}}.mut{{fill:var(--mut);font-size:12px}}
.big{{fill:var(--fg);font-size:28px;font-weight:600}}.h{{fill:var(--fg);font-size:14px;font-weight:600}}
</style>
<rect class="bg" width="760" height="460" rx="8"/>
<text x="32" y="40" class="h" style="font-size:18px">Hecate · weekly growth</text>
<text x="728" y="40" class="mut" text-anchor="end">as of {data["as_of"] or "n/a"}</text>
{kpi}
<text x="32" y="168" class="h">Top star gainers, 7 days</text>
{rows(data["gainers"], 32, 184, 316, lambda v: f"+{v:,}")}
<text x="412" y="168" class="h">Highest momentum <tspan class="mut">(2+ signals)</tspan></text>
{rows(data["momentum"], 412, 184, 316, lambda v: f"{v:.1f}")}
<text x="32" y="444" class="mut">{footer}</text>
</svg>
"""


def publish(s3, bucket, prefix, data, generated):
    for name, body, content_type in (
        ("weekly-growth.svg", render_svg(data, generated), "image/svg+xml"),
        ("summary.json", json.dumps(summary(data, generated), indent=2) + "\n", "application/json"),
    ):
        s3.put_object(Bucket=bucket, Key=f"{prefix}/{name}", Body=body.encode(), ContentType=content_type,
                      CacheControl="public, max-age=3600")


def main():
    bucket = os.environ["HECATE_PUBLIC_BUCKET"]
    connection = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"], connect_timeout=10,
    )
    try:
        data = fetch(connection)
    finally:
        connection.close()
    publish(boto3.client("s3"), bucket, "snapshot", data, datetime.now(timezone.utc))
    print(f"Public snapshot published: s3://{bucket}/snapshot/", flush=True)


if __name__ == "__main__":
    main()
