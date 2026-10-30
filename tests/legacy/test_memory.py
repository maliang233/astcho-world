import asyncio
from unittest.mock import AsyncMock, Mock

from astcho.domain.models import MemoryAtom, MemoryExtraction
from astcho.services.memory import MemoryService


def test_disabled_memory_never_reads_writes_or_calls_llm():
    vectors = Mock()
    llm = Mock(json_completion=AsyncMock())
    memory = MemoryService(vectors, llm, "test", enabled=False)
    for _ in range(6):
        memory.queue_turn("用户喜欢咖啡", "记住啦", group_id="g", user_id="u")
    assert not memory._pending
    assert memory.retrieve("咖啡", group_id="g", user_id="u", limit=10) == []
    assert asyncio.run(memory.extract("用户喜欢咖啡", group_id="g", user_id="u")) == 0
    assert asyncio.run(memory.flush()) == 0
    assert vectors.mock_calls == []
    llm.json_completion.assert_not_called()


def test_enabled_memory_still_extracts_and_retrieves():
    vectors = Mock()
    vectors.retrieve_memories.return_value = []
    llm = Mock(json_completion=AsyncMock(return_value=MemoryExtraction(
        memories=[MemoryAtom(content="用户喜欢咖啡", kind="long", importance=7)]
    )))
    memory = MemoryService(vectors, llm, "test", enabled=True)
    memory.queue_turn("用户喜欢咖啡", "记住啦", group_id="g", user_id="u")
    assert asyncio.run(memory.flush()) == 1
    vectors.add_memory.assert_called_once_with(
        "用户喜欢咖啡", group_id="g", user_id="u", kind="long", importance=7
    )
    assert memory.retrieve("咖啡", group_id="g", user_id="u", limit=10) == []
    assert vectors.retrieve_memories.call_count == 2
