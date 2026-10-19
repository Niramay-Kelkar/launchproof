"""Small application service around the graph; the CLI or an API can both call it."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from langgraph.types import Command

from .graph import build_graph, open_checkpointer
from .providers import make_services
from .schemas import FounderDecision, Idea, Settings


class Runner:
    def __init__(self, data_dir: str | Path, mode: str):
        self.data_dir = Path(data_dir)
        self.services = make_services(mode, self.data_dir)
        self.checkpoint_path = self.data_dir / "checkpoints.sqlite"

    @staticmethod
    def config(run_id: str) -> dict:
        # Reusing the same thread ID is what resumes a paused run.
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    def _result(self, graph, run_id: str, result: dict[str, Any]) -> dict:
        snapshot = graph.get_state(self.config(run_id))
        values = snapshot.values
        pending = result.get("__interrupt__") or [i for t in snapshot.tasks for i in t.interrupts]
        return {"run_id": run_id, "status": "waiting_for_founder" if pending else values.get("status"),
                "interrupt": pending[0].value if pending else None,
                "next_nodes": list(snapshot.next), "materialized": values.get("materialized"),
                "errors": values.get("errors", [])}

    def start(self, idea: Idea, settings: Settings, run_id: str | None = None) -> dict:
        run_id = run_id or str(uuid.uuid4())
        with open_checkpointer(self.checkpoint_path) as saver:
            graph = build_graph(self.services, saver)
            result = graph.invoke({"run_id": run_id, "idea": idea.model_dump(mode="json"),
                                   "settings": settings.model_dump(mode="json")}, self.config(run_id))
            return self._result(graph, run_id, result)

    def resume(self, run_id: str, decision: FounderDecision) -> dict:
        with open_checkpointer(self.checkpoint_path) as saver:
            graph = build_graph(self.services, saver)
            result = graph.invoke(Command(resume=decision.model_dump(mode="json")), self.config(run_id))
            return self._result(graph, run_id, result)

    def status(self, run_id: str) -> dict:
        with open_checkpointer(self.checkpoint_path) as saver:
            graph = build_graph(self.services, saver)
            return self._result(graph, run_id, {})

    def retry_failed(self, run_id: str) -> dict:
        """Continue after a transient/system failure from the last durable checkpoint."""
        with open_checkpointer(self.checkpoint_path) as saver:
            graph = build_graph(self.services, saver)
            result = graph.invoke(None, self.config(run_id))
            return self._result(graph, run_id, result)

    def state(self, run_id: str) -> dict:
        with open_checkpointer(self.checkpoint_path) as saver:
            return build_graph(self.services, saver).get_state(self.config(run_id)).values
