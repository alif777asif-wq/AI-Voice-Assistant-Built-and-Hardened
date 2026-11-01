import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.llm.base import parse_openai_tool_response


def test_parses_plain_text_response():
    data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Sure thing!"},
                "finish_reason": "stop",
            }
        ]
    }
    result = parse_openai_tool_response(data)
    assert result.content == "Sure thing!"
    assert result.tool_calls == []


def test_parses_single_tool_call_with_string_arguments():
    # Real Groq/OpenAI shape: arguments is a JSON *string* that needs
    # decoding, and content is null when the model calls a tool instead
    # of answering directly.
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "open_application",
                                "arguments": '{"app_name": "notepad"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    result = parse_openai_tool_response(data)
    assert result.content == ""
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_abc123"
    assert call.name == "open_application"
    assert call.arguments == {"app_name": "notepad"}


def test_parses_multiple_parallel_tool_calls():
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "adjust_volume", "arguments": '{"action": "up"}'},
                        },
                        {
                            "id": "call_2",
                            "function": {
                                "name": "search_web",
                                "arguments": '{"query": "weather today"}',
                            },
                        },
                    ],
                }
            }
        ]
    }
    result = parse_openai_tool_response(data)
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "adjust_volume"
    assert result.tool_calls[0].arguments == {"action": "up"}
    assert result.tool_calls[1].name == "search_web"
    assert result.tool_calls[1].arguments == {"query": "weather today"}


def test_malformed_arguments_json_falls_back_to_empty_dict_without_crashing():
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "file_operation",
                                "arguments": "{not valid json!!",
                            },
                        }
                    ],
                }
            }
        ]
    }
    result = parse_openai_tool_response(data)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {}
    assert result.tool_calls[0].name == "file_operation"


def test_arguments_that_decode_to_a_non_dict_falls_back_to_empty_dict():
    # A model returning a bare string or list instead of a JSON object --
    # must not crash, and must not be treated as valid kwargs.
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "search_web", "arguments": '"just a string"'}}
                    ],
                }
            }
        ]
    }
    result = parse_openai_tool_response(data)
    assert result.tool_calls[0].arguments == {}


def test_arguments_already_a_dict_is_accepted_as_is():
    # Defensive: some servers (or future Ollama native-endpoint reuse)
    # might hand back an already-parsed dict rather than a JSON string.
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "adjust_volume", "arguments": {"action": "mute"}}}
                    ],
                }
            }
        ]
    }
    result = parse_openai_tool_response(data)
    assert result.tool_calls[0].arguments == {"action": "mute"}


def test_empty_choices_does_not_crash():
    result = parse_openai_tool_response({"choices": []})
    assert result.content == ""
    assert result.tool_calls == []


def test_missing_tool_calls_key_means_plain_response():
    data = {"choices": [{"message": {"content": "no tools needed"}}]}
    result = parse_openai_tool_response(data)
    assert result.content == "no tools needed"
    assert result.tool_calls == []


def test_raw_assistant_message_is_preserved_verbatim_for_history():
    raw_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "function": {"name": "x", "arguments": "{}"}}],
    }
    data = {"choices": [{"message": raw_message}]}
    result = parse_openai_tool_response(data)
    assert result.raw_assistant_message == raw_message
