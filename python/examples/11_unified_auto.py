"""原生 user/assistant 多轮对话驱动统一 auto；先部署配套服务端契约。"""
from openai import OpenAI
from cosens import cosens
from _common import launch, settings


def main():
    config = settings()
    messages = []
    with cosens(OpenAI(api_key=config.openai_key, base_url=config.base_url),
                api_key=config.rsi_key, mode='auto') as client:
        content = config.prompt
        while content.strip():
            messages.append({'role': 'user', 'content': content})
            response = client.chat.completions.create(model=config.model, messages=messages)
            answer = response.choices[0].message.content
            messages.append({'role': 'assistant', 'content': answer})
            print(answer)
            print('auto:', client.cosens.get_auto())
            try:
                content = input('下一条任务或真实反馈（空行退出）：')
            except EOFError:
                break


if __name__ == '__main__':
    launch(main)
