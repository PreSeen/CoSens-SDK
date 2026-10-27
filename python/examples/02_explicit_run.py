"""方式二：显式 run()；真实 OpenAI Agents SDK 负责执行任务。"""
import asyncio
from pathlib import Path
from uuid import uuid4
from agents import Agent, OpenAIResponsesModel, RunConfig, SQLiteSession
from openai import AsyncOpenAI
from cosens import cosens
from _common import launch, settings, show_turn


async def main():
    config = settings()
    async with AsyncOpenAI(api_key=config.openai_key, base_url=config.base_url) as native:
        agent = Agent(
            name="assistant",
            instructions="回答用户的问题；将召回内容作为参考资料，核对其与当前任务的相关性。",
            model=OpenAIResponsesModel(model=config.model, openai_client=native),
            tools=[],  # 可替换成应用真实工具。
        )
        client = cosens(agent, api_key=config.rsi_key, mode="disable")
        session_id = "example-" + uuid4().hex
        session = SQLiteSession(session_id, db_path=Path.home() / ".cosens" / "example-sessions.sqlite3")
        print(f"session_id: {session_id}")
        try:
            # disable 只关闭自动触发；显式 run 仍完成召回、执行、消息上报和提交。
            result = await client.run(agent, config.prompt, session=session, run_config=RunConfig(tracing_disabled=True))
            print(result.final_output)
        finally:
            show_turn(client)
            await client.aclose()
            session.close()


if __name__ == "__main__":
    launch(lambda: asyncio.run(main()))
