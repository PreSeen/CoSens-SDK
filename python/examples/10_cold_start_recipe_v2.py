"""冷启动 → 初始 Recipe → 模拟用户反馈 → Recipe V2 → 修订执行。

安装：pip install 'cosens-sdk==0.3.0'
填写文件末尾的两个 API Key 后运行：python 10_cold_start_recipe_v2.py
"""

import json
import time
from uuid import uuid4

from cosens import CoSensClient
from openai import OpenAI


def run(*, cosens_api_key, model_api_key, model, model_base_url, region="us"):
    tag = uuid4().hex[:12]
    task_prompt = f"""给青禾仿真店-{tag}的 A-17 商品做一次补货建议。
最近7天销量依次为40、42、38、0、0、0、0件，当前可售库存8件，
采购系统显示在途120件、预计第4天到货。
请结合店铺已有经营记录和采购口径，告诉我这次应该订多少，以及今天优先做什么。"""

    # 模拟业务用户反馈：这些信息在首轮执行时并未提供。
    feedback = """请根据已确认的业务资料修订建议：A-17最近7天每天可售小时数为
24、24、24、0、0、0、0，后4天断货。采购提前期5天、复核周期2天、安全天数2天，
保护期为这三项之和。最小起订50件，包装倍数10件。当前可售库存8件已经包含
今天临时调入的数量；在途120件已确认，第4天到货，没有其他已确认在途或调拨。
本店订货量按日需求×保护期－当前可售库存－保护期内已确认在途计算，再向上
满足最小起订量和包装倍数。日需求须按实际可售时长核验。
以后缺少关键采购参数时应先向用户追问，不要自行假设计算。"""
    cause = """本任务依赖企业采购先验及其核验步骤。区分已有在途的到货时间与
新增采购提前期，核验可售时长、真实保护期及起订和包装约束；缺少关键参数时
先追问用户，不自行假设计算。若首轮已追问，应保留该行为。"""

    sdk = CoSensClient(api_key=cosens_api_key, region=region)
    model_client = OpenAI(api_key=model_api_key, base_url=model_base_url, timeout=None)
    atoms = sdk.sync_atoms

    def ask_model(messages):
        # 不设置输出 token 上限；流式读取直到模型完成。
        stream = model_client.chat.completions.create(
            model=model, messages=messages, temperature=0, stream=True
        )
        parts, finish_reason = [], None
        for chunk in stream:
            for choice in chunk.choices:
                if choice.delta.content:
                    parts.append(choice.delta.content)
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        answer = "".join(parts)
        if finish_reason != "stop" or not answer.strip():
            raise RuntimeError(f"模型没有生成完整答案：{finish_reason}")
        return answer

    def submit(execution, answer, name):
        artifact = atoms.artifact(name=name, content=answer)
        return atoms.submit_execution(
            execution["execution_id"], request_sha256=execution["request_sha256"],
            outcome="completed",
            artifacts=[{"artifact_id": artifact["artifact_id"], "role": "submitted_output"}],
            events=[{"id": name, "kind": "model_result", "payload": {
                "model": model, "output_artifact_id": artifact["artifact_id"]
            }}],
            trace_coverage="partial",
        )

    def wait_recipe(candidate_id):
        while True:
            recipe = atoms.get_recipe_candidate(candidate_id)
            if recipe["status"] == "candidate":
                return recipe
            if recipe["status"] not in {"queued", "running", "awaiting_parent"}:
                raise RuntimeError(f"Recipe {candidate_id} 未生成：{recipe['status']}")
            time.sleep(5)

    try:
        # 1. 首次执行：不注入 Memory 或 Recipe。
        task = atoms.create_task(source="new_task", objective=task_prompt, key=f"{tag}:task")
        execution = atoms.start_execution(
            task["task_id"], key=f"{tag}:execution", include_memory=False,
            model_api={"model": model, "api_key": model_api_key, "base_url": model_base_url},
        )
        first_answer = ask_model([
            {"role": "system", "content": "你是一名经营分析助手，不要编造企业事实；资料不足时明确说明。"},
            {"role": "user", "content": task_prompt},
        ])
        print("\n首轮答案：\n", first_answer, flush=True)

        # 2. 提交执行结果后，服务端自动创建初始 Recipe。
        first_receipt = submit(execution, first_answer, "first-answer.txt")
        v1 = wait_recipe(first_receipt["initial_recipe_candidate_id"])
        print("\nRecipe V1：\n", json.dumps(v1["recipe"], ensure_ascii=False, indent=2), flush=True)

        # 3. 提交模拟反馈，确认原因，等待 Recipe V2 生成。
        job = atoms.improve_task(
            task["task_id"], revision_id=first_receipt["revision_id"],
            feedback=feedback, key=f"{tag}:feedback",
        )
        selected = atoms.select_cause(job["job_id"], custom_reason=cause)
        v2 = wait_recipe(selected["recipe_candidate_id"])
        print("\nRecipe V2：\n", json.dumps(v2["recipe"], ensure_ascii=False, indent=2), flush=True)

        # 4. 使用服务端下发的 V2 和反馈重新执行，再提交修订答案。
        revision = atoms.start_revision(
            first_receipt["revision_id"], attribution_id=job["job_id"], key=f"{tag}:revision"
        )
        method = revision["request"]["method"]
        if method["recipe_candidate_id"] != v2["recipe_candidate_id"]:
            raise RuntimeError("修订执行没有使用 Recipe V2")
        revised_answer = ask_model([
            {"role": "system", "content": "依据用户需求和可核验的信息完成任务。修订方法：\n" +
             json.dumps(method, ensure_ascii=False)},
            {"role": "user", "content": task_prompt},
            {"role": "assistant", "content": first_answer},
            {"role": "user", "content": "\n".join(item["message"] for item in revision["request"]["feedback"])},
        ])
        receipt = submit(revision, revised_answer, "revised-answer.txt")
        print("\n修订答案：\n", revised_answer, flush=True)
        print("\n任务 ID：", task["task_id"], "Recipe ID：", v2["recipe_id"],
              "修订 ID：", receipt["revision_id"], flush=True)
    finally:
        model_client.close()
        sdk.close()


if __name__ == "__main__":
    run(
        cosens_api_key="YOUR_COSENS_API_KEY",
        model_api_key="YOUR_MODEL_API_KEY",
        model="deepseek-v4-flash",
        model_base_url="https://api.tokenriver.cn/v1",
        region="us",
    )
