# SDK 请求边界回归测试（30 个用例）

日期：2026-10-29。新增 Python 15 个、TypeScript 15 个独立测试，两版覆盖同一组 15 个场景。Python 除异步并发用例外，每个场景同时检查同步与异步原子接口。测试调用真实 SDK，只在 HTTP 边界替换外部服务，不需要真实 Key，也不依赖下游上传接口部署。

测试代码：

- [Python：test_sdk_request_regression.py](../../tests/test_sdk_request_regression.py)
- [TypeScript：sdk-request-regression.test.ts](../../../typescript/tests/sdk-request-regression.test.ts)

## 用例清单

每一行对应 Python 和 TypeScript 各一个独立用例，编号与代码中的 R01–R15 一致。

| 编号 | 场景 | 关键断言 |
| --- | --- | --- |
| R01 | 256 字符幂等键与手动重发 | 键不截断，两次发送相同正文；SDK 不另造键 |
| R02 | 257 字符幂等键 | 本地拒绝，零网络请求 |
| R03 | 显式传入空的可选幂等键 | submit/feedback/improve 不得静默去掉键后发送；Python 同步入口检查 submit |
| R04 | 幂等键含 CR/LF、NUL、DEL | 本地拒绝控制字符，零网络请求 |
| R05 | Memory ingest 的请求键 | 写入 JSON request_key，不加 Idempotency-Key；保留 durable=false、expected_revision=0 |
| R06 | 连续调用之间的 header 隔离 | 带键请求之后，普通读取和未指定键的 submit 不继承该键；保持双鉴权头 |
| R07 | HTTP 409 幂等冲突及 401 嵌套错误 | 兼容 `error`、`detail` 两种结构，保留状态、错误码、消息和 request ID 并脱敏；不自动重试 |
| R08 | HTTP 429 + Retry-After | 不自动重放写请求 |
| R09 | 上传返回 HTTP 200 但 JSON 损坏，或响应正文读取中断 | 分别报告 JSON 错误与读取失败；不泄露密钥，PUT 只发送一次 |
| R10 | 网关返回非 JSON 的 HTTP 502 | 保留 HTTP 状态和 request ID |
| R11 | 同一客户端的并发请求 | 每个请求的键、正文和结果保持对应，不串数据 |
| R12 | 上传恰好达到字节上限 | 中文按 UTF-8 的 6 字节发送；接受合法时区、闰日、微秒与显式 HTTPS 443 端口 |
| R13 | 无效上传过期时间 | 拒绝不存在的日期、非闰年 2 月 29 日、24 时和缺少时区；不消费令牌 |
| R14 | 资源 ID 含 / ? # % | 按单个路径片段编码，不改变路由或生成查询参数 |
| R15 | 上传 URL 显式指定端口 0 | 不当作 HTTPS 默认端口，不发送请求 |

## 首次运行与根因

首次运行共 24 个通过、6 个失败：Python R03/R04/R15，TypeScript R03/R04/R13。已定位并修复四类问题：

1. 可选幂等键用真假值判断，空字符串被当作未提供。改为区分“未提供”和“提供了非法值”，后者进入正常校验。
2. 两版共享键校验只检查长度和空白，未完整拒绝控制字符。增加 C0 与 DEL 校验；保留合法键原值，避免改变幂等身份。
3. Python 使用 `port or 443`，错误地将显式端口 0 归为 443。改为仅在端口为 `None` 时使用默认值。
4. TypeScript 的 `Date.parse` 会把 2 月 30 日自动滚动到 3 月。解析前检查日期格式、月份天数、闰年和时分秒，再判断过期时间。

## 运行方式

在仓库根目录运行新增 Python 用例：

```bash
python/.venv/bin/python -m pytest -q python/tests/test_sdk_request_regression.py
```

在 `typescript` 目录运行新增 TypeScript 用例：

```bash
node --import tsx --test tests/sdk-request-regression.test.ts
```

IDEA 中运行既有 `00_all_tests_CN` 即可自动包含这些用例。该总入口还会运行两版全量测试、TypeScript 构建/类型检查、国内网关只读及 Task 主链测试；真实 CN 部分读取本机配置的 Key，上传 grant 尚未部署的真实链路不在通过结论中。

全量结果见本目录的 `latest-cn/summary.json` 及各阶段日志。新增用例的通过仅证明对应 SDK 行为，不证明尚未部署的服务接口可用。

## 最终验证结果

- 新增用例：Python 15 / 15、TypeScript 15 / 15 通过。
- 全量离线回归：Python 283 / 283、TypeScript 167 / 167 通过；TypeScript 构建、类型检查通过。
- `00_all_tests_CN` 的两个离线阶段通过，但四个真实 CN 阶段均在健康检查收到 HTTP 502，整体退出码为 1，**不能宣称整体联调通过**。
- 2026-10-29 15:44（北京时间），绕过 SDK，用 curl 和同一 CN Key 的双鉴权头分别请求 `/api/health/ready`、`/api/openapi.json`、`/api/v1/memory/items?limit=1`，均返回 502。故该问题也存在于 SDK 之外的网关请求链路，具体上游故障需由服务端排查，不能靠修改客户端伪装成功或重复写入解决。
- 独立探测记录：[2026-10-29-sdk-regression-cn-probe.json](2026-10-29-sdk-regression-cn-probe.json)，包含三个 request ID，不包含 Key。

当前 SDK 测试代码与四类缺陷修复已完成；国内真实联调需服务恢复后重新运行。一次性 upload grant 的真实部署验收仍保持待验证状态。

## 美国网关补充验证

用户随后要求用其提供的 Key 直接验证美国网关 `https://rsi.cosens.ai/api`。Key 仅注入本次进程，不写入代码、环境文件或报告。

- 独立 HTTP 请求与 Python SDK 的 `/api/health/ready` 均返回 401，错误为 `unauthenticated / invalid bearer credential`；请求采用 `X-Auth-Type: api_key` 和 `Authorization: Bearer <api_key>`。
- TypeScript SDK 首次连接失败；独立复查健康检查及 OpenAPI 均定位到原生 fetch 的 `UND_ERR_CONNECT_TIMEOUT`。已核对域名、路径和 Bearer 值与用户提供的 Key 一致。本机没有设置 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY；一次不带凭证的原生 fetch 探测收到 401，说明连接并非每次都超时。
- 美国任务写入主链未运行。当前结论是验证未通过，不能把 401 或连接超时计为测试成功。已请用户确认美国区 Key 的启用情况。
- 可供排查的脱敏证据见 [US 探测记录](2026-10-29-sdk-regression-us-probe.json)。30 个新增用例与全量离线回归的通过结果不受此影响。

## 美国区新 Key 复测

用户更新本机 `python/.env` 中的 `RSI_API_KEY_US` 后重新测试。两版首次均连接到 `https://rsi.cosens.ai/api/health/ready` 并收到 HTTP 401；Python 回执中的 `detail.code=unauthenticated`、`detail.message=invalid bearer credential`。未创建美国区业务数据。随后重复验证时 Python 仍为 401，TypeScript 发生连接异常。详细时间和 request ID 见 [US 重试报告](2026-10-29-sdk-regression-us-retry.json)。

真实网关还暴露了两版 SDK 错误解析问题：错误信息放在 `detail` 对象中时未正确提取错误码和消息（TypeScript 显示 `[object Object]`）。新增离线断言先复现失败，再修复两版 HTTP 解析，并验证错误字段及 request ID 中的密钥仍会脱敏。修复后新增 30 项回归测试及两版完整离线测试均通过。401 本身来自网关凭证校验，仍需服务端确认 Key 的美国区绑定、状态及该请求的认证日志；客户端不能把它视作成功。

## 2026-10-30 美国区恢复后验证

美国网关恢复鉴权后，使用本机美国区 Key 再次测试。Python 的健康、Recipe、Memory 召回/列表和 Task → Execution → Revision 主链通过；TypeScript 主链也通过，包括相同提交正文重放和输出读回。TypeScript 初次启动执行超时后，通过已保存的原任务 ID 和原 `Idempotency-Key` 安全恢复，没有重新创建任务。结果见 [美国区验证记录](2026-10-30-us-sdk-verification.json)。

TypeScript 组合只读检查三次分别在 Memory 列表或 Memory 召回请求出现 `UND_ERR_CONNECT_TIMEOUT`；健康、OpenAPI、Recipe 和 Memory 召回在其他尝试中已完成，单独重新运行 Memory 列表调用通过。故接口功能已分别验证，但组合检查仍存在间歇性连接问题，不能称整次组合检查通过，也未对写请求增加自动重试。

同日重新运行全量离线回归：Python 283 项、TypeScript 167 项通过，TypeScript 构建和类型检查通过；其中新增请求边界用例两种语言各 15 项。

## TypeScript 响应读取错误分类

美国区复测曾出现 HTTP 200 后报告无效 JSON；现场未能确定该次响应是否因正文读取中断造成。离线模拟确认 TypeScript 会把响应流中断误判为 JSON 格式错误。现已分别处理 JSON 解析错误和响应读取异常，并扩展 R09 验证不泄露密钥、不自动重试。修复后全量离线测试再次通过（Python 283 项、TypeScript 167 项，构建和类型检查通过）。

美国区组合只读检查再次在 Memory 召回连接阶段出现 `UND_ERR_CONNECT_TIMEOUT`；独立的 Python OpenAPI 请求也出现 `ConnectError`，问题不能仅归于 TypeScript SDK。已验证的美国区两版任务主链和此前单独 Memory 列表调用结果不变，间歇性网络问题仍待环境或服务链路排查。

## 美国区网络复查

后续一次 TypeScript 组合只读检查在 `/api/openapi.json` 收到 HTTP 200 和 `application/json` 后读取正文超时；修复后的 SDK 正确报告响应读取失败。独立的原生 Node `fetch` 在另一请求中尚未收到响应头即返回 `UND_ERR_CONNECT_TIMEOUT`。这确认客户端之外也有连接失败，但不能单凭这些结果判断具体是本机网络、网关还是上游服务导致。上述问题仍需网络链路排查。
