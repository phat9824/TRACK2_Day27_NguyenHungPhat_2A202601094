from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any


def _bfs_downstream(graph: dict[str, list[str]], start: str) -> list[str]:
    """Transitive downstream nodes in BFS order, excluding `start`.

    `seen` is seeded with `start`, so a cyclic graph terminates and a node that
    is reachable by several paths is reported once, at its shortest distance.
    """
    seen = {start}
    queue: deque[str] = deque([start])
    out: list[str] = []
    while queue:
        node = queue.popleft()
        for child in graph.get(node, []) or []:
            if child not in seen:
                seen.add(child)
                out.append(child)
                queue.append(child)
    return out


def load_graph(path: str | Path) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload["dataset_lineage"] if "dataset_lineage" in payload else payload


def load_column_graph(path: str | Path) -> dict[str, list[str]]:
    """Column-level edges (`dataset.column` -> `dataset.column`)."""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("column_lineage", {})


def get_downstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    """Return transitive downstream assets in BFS order, excluding start."""
    return _bfs_downstream(graph, start)


def get_column_downstream(column_graph: dict[str, list[str]], start_column: str) -> list[str]:
    """Return transitive downstream columns in BFS order, excluding start_column.

    Dataset lineage answers "which tables break?"; column lineage answers "which
    *numbers* on which dashboard are wrong?" -- the difference between quarantining
    a whole mart and telling the CEO that one revenue tile is untrustworthy.
    """
    return _bfs_downstream(column_graph, start_column)


def get_affected_datasets(column_graph: dict[str, list[str]], start_column: str) -> list[str]:
    """Datasets touched by a column-level blast radius, in first-seen order."""
    datasets: list[str] = []
    for column in get_column_downstream(column_graph, start_column):
        dataset = column.split(".", 1)[0]
        if dataset not in datasets:
            datasets.append(dataset)
    return datasets


def extract_dbt_dataset_graph(manifest_path: str | Path) -> dict[str, list[str]]:
    """Minimal dbt manifest parser.

    It maps each dbt node unique_id to the nodes that depend on it. Students may
    enrich names, exposures, owners, columns, or OpenLineage facets.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    graph: dict[str, list[str]] = {}
    child_map = manifest.get("child_map", {})
    for parent, children in child_map.items():
        graph[parent] = list(children)
    return graph


def extract_dbt_model_graph(manifest_path: str | Path) -> dict[str, list[str]]:
    """Model-only dbt lineage, with `model.project.name` reduced to `name`.

    Tests and seeds are dropped so the graph lines up with the hand-written
    `data/baseline/lineage_graph.json` and can be diffed against it.
    """
    raw = extract_dbt_dataset_graph(manifest_path)

    def short(unique_id: str) -> str | None:
        return unique_id.split(".")[-1] if unique_id.startswith("model.") else None

    graph: dict[str, list[str]] = {}
    for parent, children in raw.items():
        parent_name = short(parent)
        if parent_name is None:
            continue
        graph[parent_name] = [name for name in (short(child) for child in children) if name]
    return graph
