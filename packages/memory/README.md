# Memory

独立安装的 Memory SDK，也是 CoSens RSI 的内部记忆依赖。分发包名和 Python 导入名统一为 `memory`。

```bash
# 在仓库根目录执行；完整 RSI 服务使用根目录的 uv sync --frozen。
uv sync --frozen --project packages/memory
uv run --project packages/memory memory-demo
```

```python
from memory import AuthContext, MemoryService, Store

store = Store.sqlite(":memory:")
store.migrate()
memory = MemoryService(store)
actor = AuthContext("local", "agent", administrator=True)
memory.create_project(actor, "example", "Example project")
memory.ingest(actor, "example", "The project owner is Alice.", request_key="example-1")
result = memory.recall(actor, "example", "Who owns the project?")
store.close()
```

`AuthContext` 由宿主认证边界构造；示例管理员用于本地项目初始化，不接受客户端自报管理员身份。

## 内容

- `src/memory/`：原有知识、来源、Wiki、图、检索与后台任务内核。
- `experiences.py`：结构化经验的持久化、版本、权限和检索。
- `recipe_experiences.py`：RSI 使用的 Recipe 经验视图。
- `cosens_api.py`：完整 RSI 服务挂载的私有 HTTP 适配器，独立 SDK 使用不需要导入它。
- `tests/`：内核及经验功能回归测试。
- `compose.yaml`：可选 PostgreSQL / Neo4j 开发环境，不会自动启动。

经验和知识共用底层 Store 与项目权限，但经验不自动写入 Wiki，也不进入普通 `recall/read_wiki`。RSI 负责按任务组合两者，普通 Agent 的知识检索语义保持原样。详见[经验接口](docs/experience-memory.md)。

合仓保留原有检索算法和默认参数，仅调整包名、导入和部署引用。已有数据库表名及图标签保留兼容，现有数据库不需要因包名调整而重建。评测工程、专用适配器、数据、配置、缓存和结果不随本仓库发布。

## 可选依赖与测试

`postgres`、`neo4j` 提供可选数据库驱动，`test` 提供 pytest 和 Ruff。

```bash
uv run --project packages/memory --extra test python -m pytest packages/memory/tests
```

真实 PostgreSQL / Neo4j 集成测试需要单独的测试数据库配置；默认不连接已有服务。
