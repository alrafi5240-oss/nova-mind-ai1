"""A scripted stand-in for the Anthropic client, so tests never hit the API."""

from types import SimpleNamespace


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def response(content, stop_reason="end_turn"):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class _Stream:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.resp.content:
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=block.text)
                )

    def get_final_message(self):
        return self.resp


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return _Stream(self.responses.pop(0))
