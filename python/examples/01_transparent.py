"""方式一：在 IDE 点击 Run；先配置 python/.env。真实模型 + 真实网关。"""
from openai import OpenAI
from cosens import cosens
from _common import launch, settings, show_turn


def main():
    config = settings()
    with OpenAI(api_key=config.openai_key, base_url=config.base_url) as native:
        # auto 复用工作流 Recipe，后台统一决定 create / revise / noop。
        # 服务端提交须只记录执行结果；见 docs/unified-auto.md。
        client = cosens(native, api_key=config.rsi_key, mode=config.mode)
        try:
            response = client.responses.create(model=config.model, input=config.prompt)
            print(response.output_text)
        finally:
            show_turn(client)
            client.close()


if __name__ == "__main__":
    launch(main)
