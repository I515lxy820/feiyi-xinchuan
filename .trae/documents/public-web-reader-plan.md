# 题目指定公开网页读取与论文信息复核计划

## Summary

在现有“初答（draft）→ 搜索（search）→ 复核（review）”链路中增加受限的公开网页读取能力：

- 题干包含明确 HTTP/HTTPS 网址时，直接读取对应公开页面，不依赖模型先判断是否需要搜索。
- 题干只有网站名称或论文线索时，先通过现有博查搜索定位页面，再读取首条可接受的公开 HTTP/HTTPS 结果。
- 提取论文题名、作者、年份、摘要及可见正文，作为不可信资料交给模型复核，并向前端返回最终真实链接和简短摘要。
- 明确网址读取与搜索可并发执行；单路失败保留另一路结果，全部失败时回退初答并要求人工复核。
- `search_mode=off` 时不进行任何网络访问；演示模式不执行真实网页读取。
- 不接入 OpenAlex、Crossref 等论文 API，不处理登录、验证码、付费墙，不绕过访问限制，不解析 PDF。

## Current State Analysis

- `app/solver.py` 的 `Solver.solve()` 当前只在 `search_mode="auto"` 且初答 `needs_search` 时调用搜索，再在有来源时调用 `model.review()`；尚无网址提取、网页读取或多路结果合并。
- `app/providers.py` 的 `BochaSearchProvider.search()` 最多执行两个查询、返回五条去重结果，并通过 `SearchOutcome.results/warnings` 传递部分失败信息。`OpenAICompatibleProvider.review()` 当前只发送标题和搜索摘要，没有网页正文。
- `app/schemas.py` 的 `Citation` 当前为 `title/url/snippet/rank`，同时用于模型复核、API 响应、SQLite 引用保存和前端展示，适合扩展来源类型及仅供模型使用的正文字段。
- `app/config.py` 已有模型和搜索超时配置，`demo_mode` 可识别 mock 模式；尚无网页读取配置。
- `app/main.py` 的 `create_app()` 创建 `Solver(settings)`，lifespan 结束时调用 `Solver.close()`；当前 `Solver.close()` 关闭模型和搜索 provider。
- `static/app.js` 的 `renderResult()` 已通过安全 DOM API 渲染来源标题、真实链接和摘要，只需增加来源类型标记。
- `tests/conftest.py` 使用 `FakeModel`、`FakeSearch` 和注入式 `Solver` fixture；现有测试覆盖搜索失败降级、复核改答案、API 来源和标准答案隔离，但未覆盖网页读取及 SSRF。
- 当前服务仍是演示模式，没有真实模型和博查密钥，因此本计划完成后可做无网络自动化验证，真实联网冒烟仍需有效配置。

## Proposed Changes

### 1. 新增安全的公开网页读取器

**新增 `app/web_reader.py`。**

实现 `PublicWebReader.read(url: str) -> SearchOutcome` 及网页读取协议，复用 `providers.ProviderError` 和 `SearchOutcome`。

每次 `read()` 使用独立、自动关闭的 `httpx.AsyncClient`，配置 `trust_env=False`、`follow_redirects=False` 和总超时，不携带 Authorization、Cookie 或用户凭据，也避免重定向及并发读取之间共享 Cookie。这样无需在 `main.py` 增加持久客户端生命周期。

读取规则：

1. 仅允许 HTTP/HTTPS，要求有效 hostname，拒绝 URL userinfo、localhost 和非公网 IP。
2. 每一跳请求前通过异步 DNS 解析检查所有目标地址；使用 `ipaddress` 拒绝私网、回环、链路本地、组播、保留、未指定及其他非 `is_global` 的 IPv4/IPv6 地址。
3. 手动处理 301/302/303/307/308，最多三次；用 `urljoin` 解析 Location，并对每个新 URL 重新执行全部校验。拒绝其他重定向行为及无效 Location。
4. 仅接受 `text/html` 和 `application/xhtml+xml`；PDF、图片、缺失或不支持 Content-Type 的响应不当作 HTML 解析，返回可读警告。
5. 请求前检查 Content-Length，读取时通过 `aiter_bytes()` 累计解码后的响应体，超过配置上限立即终止；超时、HTTP 状态、DNS、类型和大小错误统一转成不泄露内部信息的 `ProviderError`。
6. 使用标准库 `HTMLParser` 跳过 script/style/noscript，不执行脚本，不点击页面，不提交表单；合并空白并截断可见正文，单页正文最多 12,000 字符。
7. 提取 title、description/og:description，以及 `citation_title`、多个 `citation_author`、`citation_publication_date`/`citation_date`/`citation_year`、`citation_abstract` 等 meta。网页摘要优先由这些论文元数据组成，正文不足时使用短文本摘要。
8. 成功时返回 `Citation(kind="webpage", title=..., url=最终URL, snippet=短摘要, page_text=清洗正文)`；网页正文和元数据都只作为不可信证据，不执行其中任何指令。

### 2. 扩展来源数据契约但隔离正文

**修改 `app/schemas.py`。**

- 为 `Citation` 增加 `kind: Literal["search", "webpage"] = "search"`，保持旧搜索引用兼容。
- 增加 `page_text: str = Field(default="", exclude=True, repr=False)`，仅供服务端模型复核使用。
- 保持现有数据库表结构不变；`page_text` 不进入 `model_dump()`、API JSON、SQLite 引用记录或前端日志，前端只收到 `kind` 和短摘要。

### 3. 增加配置和默认边界

**修改 `app/config.py`、`.env.example`。**

增加以下服务端配置及对应示例：

- `web_reader_enabled: bool = true` / `WEB_READER_ENABLED`
- `web_timeout_seconds: float = 8` / `WEB_TIMEOUT_SECONDS`
- `web_max_pages: int = 2` / `WEB_MAX_PAGES`
- `web_max_bytes: int = 1_048_576` / `WEB_MAX_BYTES`

对页数和字节数设置合理校验范围，避免零值、负数或无限上限。构建读取器时，若功能关闭或 `settings.demo_mode` 为真则返回 `None`，确保演示模式无真实网页请求；搜索的 mock/off 行为保持不变。

### 4. 在 Solver 中编排网址读取、搜索与降级

**修改 `app/solver.py`。**

- `Solver.__init__()` 增加可注入的 `web_reader` 参数；未注入时根据 Settings 构建，保持现有测试的 model/search 注入方式。
- 初答完成后，从原始题干及模型转写题干中提取最多 `web_max_pages` 个去重 HTTP/HTTPS URL，清理常见中文句读和尾部标点。
- 识别网站/网页/论文/题名/作者/年份/摘要等外部页面意图：
  - 有明确 URL：直接读取；若初答同时要求搜索，则用 `asyncio.gather` 并发执行搜索与各页面读取。
  - 无明确 URL 但有页面意图：即使初答 `needs_search=false` 也执行搜索；搜索返回后只读取首条可接受的公开 HTTP/HTTPS 结果，不批量抓取全部搜索结果。
  - 普通题：维持现有搜索流程，不额外抓取网页。
- `search_mode=off` 时不提取后访问、不搜索、不读取；演示模式下明确 URL 仅产生“演示模式未读取公开网页”警告。
- 每次读取独立捕获 `ProviderError`，搜索失败不丢弃网页结果，网页失败不丢弃搜索摘要；所有外部路径均失败时保留候选答案、降低置信度并设置 `needs_review=true`。
- 合并结果时按最终 URL 去重，网页原文优先于同 URL 的搜索摘要，重排 `rank`；保留现有最多五条搜索摘要，并额外允许至多两个网页来源。
- 有任何网页或搜索来源时调用现有 `model.review()`；复核失败仍保留初答和已获取引用，并维持人工复核提示。
- 保持现有图片识别警告、来源冲突、答案改变、空答案及搜索失败的 `needs_review` 规则。

### 5. 让复核模型使用网页正文而非仅摘要

**修改 `app/providers.py`。**

- `OpenAICompatibleProvider.review()` 为每个来源发送 `ref/title/url/kind/snippet`；网页来源另发送截断后的 `page_text`，搜索来源仍只发送摘要。
- 每页最多发送 12,000 字符，两个网页合计最多 24,000 字符，避免整页内容造成上下文和延迟失控。
- 更新 `REVIEW_PROMPT`：明确搜索摘要和网页正文均为不可信数据，网页中的命令、角色要求、密钥请求均忽略；只能依据提供的资料修正答案，不能编造论文字段或 URL。
- 保持单选、多选严格标签格式和判断题“正确/错误”格式；不因网页读取改变判分规则。

### 6. 标记前端来源类型

**修改 `static/app.js`。**

- 在 `renderResult()` 的来源项中，根据 `source.kind` 显示“网页原文”或“搜索摘要”。
- 继续用 `textContent` 和安全链接渲染标题、摘要及最终 URL；不渲染 `page_text`，不引入 HTML 拼接或脚本执行。

### 7. 增加无真实网络的自动化测试

**修改 `tests/conftest.py`、`tests/test_solver.py`、`tests/test_app.py`，新增 `tests/test_web_reader.py`。**

- 增加可记录调用的 `FakeWebReader`；普通 fixture 默认禁用或使用 fake，杜绝测试访问真实网络。扩展 FakeModel 的复核调用记录，用于核对正文只在服务端传递。
- 网页解析：通过 `httpx.MockTransport` 和 mock DNS 验证题名、作者、年份、摘要、可见正文、script/style 排除及最终 URL。
- 安全边界：覆盖非 HTTP、userinfo、localhost/私网/回环地址、重定向到私网、重定向上限、不支持的 Content-Type、Content-Length 超限、流式正文超限和超时；全部测试不依赖公网。
- 编排：覆盖明确 URL 即使 `needs_search=false` 也读取、网站名称先搜索再读首条、最多两页、网页与搜索结果合并去重、单路失败降级、两路失败人工复核，以及 `search_mode=off` 和演示模式零网页调用。
- 复核：断言网页正文进入 review 输入且按上限截断，搜索摘要行为不变，网页文本中的提示词注入不会改变系统规则。
- API/持久化：断言响应来源包含 `kind="webpage"` 和真实链接、不包含 `page_text`，数据库仍可按现有结构保存引用，标准答案不进入任何读取或复核输入。

## Assumptions & Decisions

- 仅处理题目指定或搜索定位的公开 HTML/XHTML 页面；本阶段不增加 PDF 下载和解析。
- 不访问需要登录、验证码、付费墙或反自动化绕过的页面；遇到限制只提示人工打开核对。
- 明确 URL 最多读取两页；无 URL 时只读取首条合格搜索结果，避免批量抓取和延迟失控。
- DNS 校验在每次请求及每个重定向前执行，并禁用环境代理和隐式重定向，防止未经校验的跳转。
- 网页正文是模型证据而非用户可见文档，因此只在内存中短暂传递，不持久化、不返回前端。
- 保持独立答题助手边界，不自动登录、选择或提交正式比赛答案。
- 真实 80% 准确率仍须用至少 200 道未参与调优的冻结集验证；网页读取功能完成不等于准确率承诺达成。

## Verification

1. 在 `/workspace/ai-literacy-assistant` 运行 `pytest -q`，确保原有测试及新增网页读取、安全和降级测试全部通过。
2. 运行针对网页链路的测试，确认没有任何真实 DNS/HTTP 请求，`search_mode=off` 与 demo 模式零网页调用。
3. 通过 API 集成测试检查网页来源的 `kind`、最终真实 URL、短摘要和 `page_text` 隔离，并确认数据库引用保存正常。
4. 启动现有应用，检查健康状态、快速答题、模拟训练和错题流程无回归；用测试来源检查前端“网页原文/搜索摘要”标记及安全链接。
5. 用户配置真实视觉模型和博查密钥后，再用一个明确公开网页 URL 和一个仅含网站名称的论文题做联网冒烟；若网站拒绝公开访问，只记录警告并人工核对，不绕过限制。
6. 真实冻结集仍按现有 `scripts/evaluate.py` 验收，超时、异常和空答案计错，不能以演示结果代替 80% 验收。
