"""Tool calls written as text (small local models) are recovered before routing."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from coder_agent.graph.repair import repair_tool_calls
from tests.fakes import scripted
from tests.test_graph import run

TOOLS = {"read_file", "edit_file", "run_command"}


def test_bare_json_object_becomes_a_tool_call() -> None:
    msg = AIMessage(content='{"name": "read_file", "arguments": {"path": "durations.py"}}')
    out = repair_tool_calls(msg, TOOLS)
    assert [(c["name"], c["args"]) for c in out.tool_calls] == [("read_file", {"path": "durations.py"})]
    assert out.content == ""
    assert out.tool_calls[0]["id"].startswith("repaired_")


def test_prose_fence_and_tags_are_stripped_and_prose_kept() -> None:
    text = (
        "The units are swapped. Fix it and run the tests.\n\n"
        "```json\n<tool_call>\n"
        '{"name": "edit_file", "arguments": {"path": "d.py", "old_string": "\\"m\\": 1", '
        '"new_string": "\\"m\\": 60"}}\n'
        "</tool_call>\n```\n"
        '{"name": "run_command", "parameters": {"command": "pytest -q"}}'
    )
    out = repair_tool_calls(AIMessage(content=text), TOOLS)
    assert [c["name"] for c in out.tool_calls] == ["edit_file", "run_command"]
    assert out.tool_calls[0]["args"] == {"path": "d.py", "old_string": '"m": 1', "new_string": '"m": 60'}
    assert out.tool_calls[1]["args"] == {"command": "pytest -q"}
    assert out.content == "The units are swapped. Fix it and run the tests."


def test_openai_shape_with_stringified_arguments() -> None:
    text = '{"function": {"name": "read_file", "arguments": "{\\"path\\": \\"a.py\\"}"}}'
    out = repair_tool_calls(AIMessage(content=text), TOOLS)
    assert out.tool_calls[0]["args"] == {"path": "a.py"}


def test_json_that_is_not_a_known_tool_is_left_as_text() -> None:
    for text in (
        '{"name": "delete_everything", "arguments": {}}',  # unknown tool
        '{"name": "read_file"}',  # no arguments object
        '{"name": "read_file", "arguments": "not json"}',
        'Here is the config: {"debug": true, "level": 3}. Done.',
        "No braces at all, just a summary.",
    ):
        msg = AIMessage(content=text)
        out = repair_tool_calls(msg, TOOLS)
        assert out.tool_calls == [] and out.content == text


def test_structured_calls_and_metadata_are_untouched() -> None:
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    meta = {"model_name": "qwen2.5-coder:7b"}
    text = '{"name": "read_file", "arguments": {"path": "x"}}'
    structured = AIMessage(
        content=text,
        tool_calls=[{"name": "edit_file", "args": {"path": "y"}, "id": "call_9"}],
        usage_metadata=usage,
        response_metadata=meta,
    )
    assert repair_tool_calls(structured, TOOLS) is structured
    repaired = repair_tool_calls(
        AIMessage(content=text, usage_metadata=usage, response_metadata=meta), TOOLS
    )
    assert repaired.tool_calls[0]["name"] == "read_file"
    assert repaired.usage_metadata == usage
    assert repaired.response_metadata == meta


def test_act_node_executes_a_text_tool_call() -> None:
    reply = 'Calling the tool now.\n{"name": "echo", "arguments": {"text": "hi"}}'
    llm = scripted("plan", reply, "finished")
    out = run(llm)
    msgs = out["messages"]
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    assert msgs[1].content == "Calling the tool now."
    assert msgs[2].content == "echo:hi"
    assert msgs[2].tool_call_id == msgs[1].tool_calls[0]["id"]
