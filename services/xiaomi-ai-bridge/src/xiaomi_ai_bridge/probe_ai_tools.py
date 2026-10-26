from __future__ import annotations

import asyncio

from .ai import AIClient
from .config import Settings


async def async_main() -> None:
    client = AIClient(Settings.from_env())
    await client.start()
    try:
        result = await client.complete(
            [
                {
                    "role": "user",
                    "content": "请调用工具查询测试灯状态，不要直接回答。",
                }
            ],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "probe_device_state",
                        "description": "读取测试灯状态",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "device": {
                                    "type": "string",
                                    "enum": ["测试灯"],
                                }
                            },
                            "required": ["device"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        )
        print(f"tool_call_count={len(result.tool_calls)}")
        print("tool_names=" + ",".join(call.name for call in result.tool_calls))
        print(f"content_present={bool(result.content)}")
    finally:
        await client.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
