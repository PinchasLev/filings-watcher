"""Smoke test: prove the LangGraph + LangSmith + Anthropic wiring works end-to-end.

Runs a trivial single-node LangGraph that asks Claude one question, traces the
execution to LangSmith, and prints a link to the trace. If this passes, the
agent loop is alive: Anthropic API key works, LangSmith project receives traces,
LangGraph can compose nodes.

All config / secrets are read through `filings_orchestrator.config` so the
source can be swapped from `.env` to AWS SSM Parameter Store without touching
call sites here.
"""

from __future__ import annotations

import os
import sys
from typing import TypedDict

from langchain_anthropic import ChatAnthropic
from langgraph.graph import END, START, StateGraph

from filings_orchestrator.config import MissingConfigError, load_config


class State(TypedDict):
    question: str
    answer: str


def ask_claude(state: State) -> State:
    """Single LangGraph node: ask Claude the question, store the answer."""
    model = ChatAnthropic(model_name="claude-haiku-4-5-20251001", timeout=30, stop=None)
    response = model.invoke(state["question"])
    content = response.content
    answer = content if isinstance(content, str) else str(content)
    return {"question": state["question"], "answer": answer}


def build_graph() -> StateGraph[State, None, State, State]:
    graph: StateGraph[State, None, State, State] = StateGraph(State)
    graph.add_node("ask_claude", ask_claude)
    graph.add_edge(START, "ask_claude")
    graph.add_edge("ask_claude", END)
    return graph


def main() -> None:
    try:
        config = load_config()
    except MissingConfigError as e:
        sys.exit(
            f"{e}\nCopy orchestrator/.env.example to orchestrator/.env and fill in real values."
        )

    # LangChain/LangSmith integrations expect their inputs as env vars, so
    # re-export from the validated Config object. This keeps the config seam
    # as the single source of truth while still satisfying the libraries.
    os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key
    os.environ["LANGSMITH_API_KEY"] = config.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = config.langsmith_project
    os.environ["LANGSMITH_TRACING"] = "true" if config.langsmith_tracing else "false"

    print(f"LangSmith project: {config.langsmith_project}")
    print(f"LangSmith tracing: {config.langsmith_tracing}")
    print()

    app = build_graph().compile()
    result = app.invoke({"question": "In one sentence, what is an SEC Form 8-K?", "answer": ""})

    print("Question:", result["question"])
    print("Answer:  ", result["answer"])
    print()
    print("Trace visible at: https://smith.langchain.com/")


if __name__ == "__main__":
    main()
