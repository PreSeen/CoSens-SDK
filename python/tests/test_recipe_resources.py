from pathlib import Path

from cosens.rsi import RSIClient
from cosens.rsi._queue_client import RSIClient as QueueClient
from cosens.rsi.recipe_package import read_recipe_package, stage_recipe_package_files

from test_rsi_v2 import FileRuntime, material, policy, settings


def test_local_update_keeps_base_resources_through_generation_test_and_frozen_zip(tmp_path):
    data_dir = tmp_path / "data"
    client = QueueClient(data_dir=data_dir, config=settings(), auto_start=False)
    content = {"name": "Report", "description": "Make a report", "instructions": "Write a report."}
    base = client.recipe_insert(
        content, "---\nname: Report\ndescription: Make a report\n---\nWrite a report.\n",
    )
    base_id = base["active_version_id"]
    resources = {
        "assets/data.bin": bytes([0, 255]),
        "scripts/report.py": b'print("fixture")\n',
        "templates/example.txt": b"Reusable template\n",
    }
    stage_recipe_package_files(data_dir / base["storage_path"] / base_id, resources)
    execution = client.execution_create(
        material_ids=[material(client, tmp_path)["id"]],
        policy=policy(),
        base_recipe_version_id=base_id,
    )

    class ResourceRuntime(FileRuntime):
        def generate(self, context, files, trace_path):
            assert context["base_recipe"] == content
            assert context["base_recipe_resources"] == [
                {"path": "assets/data.bin", "size": 2},
                {"path": "scripts/report.py", "size": 17, "text": 'print("fixture")\n'},
                {"path": "templates/example.txt", "size": 18, "text": "Reusable template\n"},
            ]
            return super().generate(context, files, trace_path)

        def execute(self, context, files, workspace, trace_path):
            assert context["package_files"] == sorted(resources)
            assert {name: (workspace / name).read_bytes() for name in resources} == resources
            return super().execute(context, files, workspace, trace_path)

    assert client.run_pending(runtime=ResourceRuntime()) == {"processed": 1}
    result = client.execution_get(execution["id"])
    assert result["recipe_id"] == base["id"]
    assert result["result"]["published"] is True
    assert [item["locator"] for item in result["test_results"][0]["artifacts"]] == ["test/answer.txt"]

    task = RSIClient(data_dir=data_dir).task_get(execution["id"])
    package = read_recipe_package(Path(task["artifact"]["zip_path"]).read_bytes())
    markdown = package.pop("RECIPE.md")
    assert b"Write 42 into answer.txt" in markdown
    assert package == resources
