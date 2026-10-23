# 关键词研究 保存关键词 生成报告 首条原生流程

2026-10-14。参考OpenSEO d7ea785；目标Octop 5acbaf4。用户明确：无OpenSEO账号，采用本地参考执行与离线样本，本轮不产生API费用。原生实现直接使用供应商，运行不依赖OpenSEO。

## 逆向结论与证据

REA capture_process_scenario 执行固定版本的9个真实源码模块，23项检查通过，exit0。证据ID ev_9ed43ef5c70697930e77e0ee8c8430388ce6277d7de13152096e96e7232ae55c。供应商、缓存与repository是显式替身；报告HTML来自fixture。该结果证明受控源码行为，不证明线上页面、真实供应商、数据库事务或LLM报告质量。

链路：research执行并缓存指标 → 用户选词后保存membership/metrics/tags → Agent读取研究及已保存词、编写HTML → save_report验证和保存。保存关键词不重新查询供应商；save_report不调用LLM。来源见 upstream-contract.md 与 reference-result.json。

研究：trim+lowercase；auto将suggestions/ideas交错去重，各ceil(limit/2)；少于5个非seed词再追加related，恰好5个不追加。一个seed的两条源请求任一失败，该seed失败，但已完成调用的费用仍保留。批量seed可部分成功。空值与0分开。共享词量分组只影响展示。

保存：project+keyword+国家+语言唯一；重复保存保持ID，可更新所选指标和标签；不传指标不清空旧值。append取并集、replace非空。原savedCount是输入数，原生改成明确created_count/updated_count/saved_count，并保留原研究observed_at和run_id。

报告：500000 UTF8 bytes，title120、summary2500、skill60；完整HTML。原更新覆盖内容，本地每次保存独立不可变report_id；保留source run及关键词快照和hash。公开分享不进入此切片。

## 本轮实现边界

先完成国家级Labs关键词（无城市localName、无Google Ads fallback），mode auto/related/suggestions/ideas，limit150/300/500，clickstream/group options，默认均false。有明确不支持项时拒绝，不伪造指标。市场使用明确项目默认值及US/UK/CA/AU及英语en，默认US2840/en。

页面完成创建项目（绑定自己的Agent）→明确发起研究→选择和保存词/标签→打开本地Octop对话生成报告→私有报告列表和静态预览。Agent首轮提供读取已研究证据、读取已保存词和保存HTML报告工具；真实收费研究由页面明确触发，避免仅凭模型参数授权付费。离线E2E通过注入供应商fixture，不在生产服务内设置静默demo数据。

报告HTML作为有严格字节上限的文档数据保存在SEO报告表，元数据和内容分开读取；这样本切片无需依赖运行中的Agent工作区，也不把正式版本放到可编辑的文件路径。文件导出是副本。该选择替代总方案中首版先存工作区文件的建议；后续大产物再通过BackendWorkspace。Agent生成HTML使用现有模型执行能力，测试使用明确替身，不声称已验证真实模型。

## 原生接口契约

所有路径前缀 /api/seo，身份取current_user，业务服务重复校验项目与Agent归属。

- GET /projects -> Project[]；POST /projects {name,domain,agent_id,location_code=2840,language_code=en} -> Project。Project字段 project_id,name,domain,agent_id,location_code,language_code,created_at。
- GET /provider -> {configured:boolean}；PUT /provider {login,password} -> {configured:true}。每用户加密，任何GET不返回凭证；仅固定api.dataforseo.com，无任意base_url。
- POST /projects/{id}/research {keywords:[1..5],mode,result_limit,clickstream,group_keywords,idempotency_key,confirm_paid} -> ResearchRun。confirm_paid必须true；未配置凭证则明确失败。无自动供应商重试。GET /projects/{id}/runs/{run_id}、GET /projects/{id}/runs -> 读回。
- ResearchRun字段 run_id,project_id,status(running/completed/partial/failed/unknown),observed_at,location_code,language_code,results:[{seed,ok,source,used_fallback,rows,error?}],cost_usd(number|null),cached,error?。
- KeywordRow字段 keyword,search_volume,cpc,competition,keyword_difficulty,intent,trend:[{year,month,search_volume}]。数值可null，intent unknown保留。
- POST /projects/{id}/keywords {run_id,keywords:[1..500],tags:[<=20],tag_mode:append|replace} -> {saved_count,created_count,updated_count}。指标从已验证的该run所选行取得，不信任前端上传指标。原子写入，空白归一化后全空拒绝，replace无有效标签拒绝。
- GET /projects/{id}/keywords?search=&page=1&page_size=50 -> {rows:SavedKeyword[],total_count}。SavedKeyword扩展 keyword_id,location_code,language_code,observed_at,source_run_id,tags。
- GET /projects/{id}/report-context?run_id= -> {project,run,saved_keywords,instructions,context_hash}；生成与来源日期、缺失值、计费结果相关的Agent上下文。
- POST /projects/{id}/reports {run_id,context_hash,title,summary,html,skill=keyword-research} -> ReportMetadata；每次新ID且无更新接口。HTML不进入列表。GET /projects/{id}/reports -> ReportMetadata[]；GET /projects/{id}/reports/{id} -> metadata+html。所有读回校验项目和hash。ReportMetadata字段report_id,project_id,run_id,title,summary,skill,created_at,content_hash,size_bytes。

相同幂等键及参数返回已有run；同键不同参数409。不同幂等键的并发相同输入尚未合并，缓存产生前仍可能分别计费，不能宣称全局防重复付费。缓存24小时，命中保留原observed_at，不延长数据新鲜度。研究run建立后才调用供应商。每个供应商调用单独落账，HTTP200也检查task status和cost。未知结果不盲重试，缺少费用为null。缓存命中不买新数据，key包含owner/project/market/options。同步研究本切片受明确请求数量限制；断连可用run_id读回，读回超过20分钟的遗留running标unknown/interrupted而不冒充成功。

测试约定沿既定闭环：业务服务+真实临时DB、HTTP权限接口、前端选择保存报告用户路径；只mock外部网络/模型。使用原版fixture校验纯规则，不断言内部私有调用顺序。事务和权限用本地真实DB验证。构建后再以REA捕获本地UI的离线闭环。

## 保存与报告的本地强化

统一UI/tool保存上限500；项目/市场唯一；指标来源日期不可换成保存时间；原子保存标签；报告不可变、UTF8 byte cap、SHA256读回；context_hash必须匹配生成前证据，保存事务中再核验选词快照，变化返回409要求重读和重写；预览sandbox无脚本/表单，限制外部资源。任何AI结论须有原研究证据，不把模板示例当真实报告。

## 本轮不宣称完成的内容

OpenSEO登录后界面现场取证、真实DataForSEO费用与供应商响应质量、Google OAuth、城市搜索量、Google Ads国家fallback、完整技术审计/排名/AI、公共分享。本切片交付必须清楚标出离线与真实验证层级。

## 2026-10-14 本地验收结果

- 固定原版9模块受控回放：23/23；真实Zod和TypeScript，供应商/缓存/repository替身在reference-result.json逐项列明。
- `make all` 格式、Ruff、mypy（569源文件）通过；全库 pytest 4325 passed、17 skipped、3 failed。三项均为tests/unit/test_fnos_native_stop.py，在未修改5acbaf4基线独立执行同样3 failed/5 passed，因此不是本次SEO回归；完整门禁不能标为全绿。
- SQLite 20→21迁移、重复迁移与外键检查通过；PostgreSQL仅静态审查，未运行真实PostgreSQL。
- 前端tsc/Vite构建通过；SEO与Workbench相关测试22项通过。
- REA原生研究：ev_8b7cd7f3ed24d528d4d3e1ddee44463eecae8c7bc4815cb66e6cd249eaad406d。真实HTTP和SQLite，MockTransport返回6个样本词；模拟$0.02，实际$0。
- REA保存6词及offline-verified标签、打开正确Agent新线程与预填草稿：ev_5472bce0a01153d16b628f91dde0c67002eee956dc463b2612de2cf9879c6a80；无自动发送。
- REA报告样本保存/读回：ev_0613c2562f9dacc436d5b29fd1e9cf1588969772cb8e884052880b113f1e9a1a，正文为明确固定fixture，经真实报告HTTP接口写入。没有真实LLM生成，也没有把该步骤称为真实Agent工具链E2E。工具身份及上下文约束另有单元测试。
- REA私有报告列表/静态预览：ev_5b839530f36b77e571877ab3a45a58d1a7513b34826650525566b6c23a73db23，截图可读，最终无page-error。REA存储注入在sandbox iframe遇到protocol_error，最终改用仅本地测试实例的登录入口，空storage及service_workers=allow成功，应用sandbox未放宽。

真实供应商、真实模型质量和真实PostgreSQL为后续验收项。未提交、未推送、未部署；没有迁移用户正在使用的数据库。

## 2026-10-14 部署前修订

审计后修了三个阻塞部署的问题，代码改为基于已部署的 fork 分支 `role-visible-pages`（e5918582）。上面「本地验收结果」里的 20→21 迁移已不再适用。

- **只给管理员。** `/api/seo/*` 全部要求管理员角色；服务层在访问项目时再查一次，Agent 工具只经过这一处；只有管理员拥有的 Agent 才会挂上 SEO 工具。工作台页面登记为 `adminOnly`：非管理员看不到这个标签，直接打开链接会被拒绝，也不会因为这个页面在侧栏多出「工作台」。
- **不占迁移编号。** 表结构放在 `migrations/seo_workbench.sql` 和 `.pg.sql`，由 `run_migrations()` 在编号迁移之后每次启动幂等执行（`_ensure_seo_schema`），版本号保持 20。原因是迁移程序只记一个版本号：fork 占用 021，上游将来的 021 在已升级的库上就会被跳过。被旧版标成 21 的库会被收回到 20。
- **Agent 工具结果有大小上限。** 每个结果不超过 60,000 字符（框架会把超过约 80,000 字符的结果转存成文件，模型只能看到前 1000 个字符）。`seo_get_report_context` 先给 `context_hash` 和写作要求，再给运行摘要、前 50 个已保存词和按搜索量排序的前 50 行研究结果；其余用 `seo_list_saved_keywords` 和新增的 `seo_list_research_rows` 按 `offset` / `next_offset` 读取。`seo_list_research_runs` 只返回摘要，`seo_list_reports` 也分页。趋势写成 `[年, 月, 搜索量]`。`context_hash` 仍覆盖完整证据。

验证：`make all` 中格式、Ruff、mypy（569 个文件）通过，pytest 4330 通过、18 跳过、3 失败，失败的仍是本机缺 Linux `/proc` 的飞牛 NAS 测试 `test_fnos_native_stop.py`；前端 `tsc -b`、`npm run build` 通过，工作台与 SEO 相关 vitest 通过；本机临时 PostgreSQL 17 上建表、重复启动、研究、并发保存、报告保存均通过，版本号保持 20。真实模型生成报告仍未验证。

## 2026-10-15 补齐 R1

用户选了「先补齐 R1」，并定了三件事：每个种子词一个搜索标签；删除专家时保留项目、解除绑定；研究日志由专家写。

- **SERP 分析。** 按 OpenSEO 的 SERP 卡片移植：`POST /serps` 调 DataForSEO `serp/google/organic/live/advanced`，桌面端用 windows、移动端用 android，深度只开放 20 和 100。`rank` 是全部结果中的位置，`organic_rank` 只算自然结果。12 小时内的重复请求直接复用已保存的快照、不收费；深度记在快照里而不在缓存键里，所以已有 100 条的快照可以回答 20 条的请求。空结果（供应商返回 No Search Results，照样计费）不会被缓存复用，避免一次偶发的空结果盖住之前的好结果；缓存副本本身不参与查找，不会延长数据的新鲜度。超过 5 分钟仍是 running 的记录标为 unknown。购买只在页面上手动确认后发生；专家只有只读的 `seo_list_serps` / `seo_get_serp`。
- **多搜索标签。** 一次付费研究按种子词拆成多个标签，同一浏览器会话内按项目保留，最多 20 个，超出时关掉最旧的；关闭当前标签时选右边一个，没有就选左边。标签只指向已经买过的研究，所以切换标签从不触发付费。页面只加载当前标签那次研究的完整数据，历史列表只读摘要。
- **项目背景。** 四个固定栏目（业务概况、当前目标、定位、写作偏好）、最多 20 个自定义栏目、最多 100 个竞争对手、最多 100 个关键页面，每段文字上限 4000 字。每次修改是一批操作，先整批校验再写入，任何一条不合格整批都不写，错误里写明是第几条（`updates[i]`）。竞争对手域名统一成不带 www 的主机名；关键页面地址统一成 https、去掉 www 和 `#` 后的部分，保留路径和查询参数。删除不存在的条目视为已完成。研究日志由专家在保存报告后追加，只保留 90 天，页面上只能查看和删除。
- **报告证据。** 项目背景计入报告的 `context_hash`，背景改了以后，按旧证据写的报告会被拒绝保存；研究日志不计入，因为专家在保存报告之后才写日志。
- **删除专家。** `seo_projects.agent_id` 改为 `ON DELETE SET NULL`。项目、研究、关键词、SERP、背景和报告都保留；解绑期间可以照常查看，不能生成报告；在页面上绑定另一个专家即可恢复。权限只看管理员身份和项目归属，不再要求绑定的专家仍然存在。
- **加载慢。** 研究记录里新增一份摘要（`seo_runs.summary`），历史列表只读摘要和按记录汇总的费用，不再把所有研究的全部关键词行一次读出来。

表结构仍在 `seo_workbench.sql` / `.pg.sql`，版本号保持 20。目前没有任何数据库建过 SEO 表，所以这次直接改了建表语句；**上线以后再改结构，必须写成可重复执行的 ALTER**。

验证：`tests/unit/seo` 36 项、SEO 集成测试 2 项、建表测试 2 项通过；前端 `tsc -b`、eslint 通过，SEO 相关 vitest 15 项通过（含分标签、SERP 只在确认后购买且失败重试复用同一请求标识、项目背景按批保存、解绑与重新绑定）；本机临时 PostgreSQL 17 上跑通删除专家后项目保留、重新绑定、SERP 缓存与重放、并发写背景、整批拒绝、报告证据含背景不含日志。真实 DataForSEO 和真实模型仍未调用。

## 2026-10-16 R2：技术审计

用户定了 R2 两个模块都做、审计先交；Google 搜索表现（GSC + GA4）要等用户提供 Google OAuth 客户端，排在后面。

- **移植范围。** 参考 OpenSEO d7ea785：页面解析（page-analyzer）、网址规则（url-utils）、robots（robots-parser 3.0.1）、30 类问题规则（page-reporters、multipage-checks）、限流退避（crawl-throttle）、站点地图发现。页面解析、网址规范化、同源判断和 robots 用上游代码实际跑出的样本逐项对照（`tests/unit/seo/audit/openseo_golden.json`，59 个），全部一致；规则和限流移植了上游的测试用例。
- **怎么跑。** 服务器进程内的后台任务，每个进程同时最多 2 个审计，每个项目同时只能有 1 个。以 `Octop-SEO-Audit/1.0` 身份一次抓一页，间隔 1 秒；遇到 429 按 Retry-After 或 30 秒倍增退避，同一网址最多重试 3 次，连续 4 次被拒或累计冷却超过 30 分钟就停止，并标出「被限流、结果不完整」。页数上限 10–200，默认 50；起始网址默认是项目域名。免费，不调用付费数据源。
- **断点续跑。** 待爬队列、页面和问题边爬边写库。重启或部署时把任务交还，下一个进程从断点继续；每次写库都核对认领标记，用户取消、删除或任务被别的进程接手后，旧进程在下一次写入时停下，不会写出重复或残留数据。心跳 20 秒一次，90 秒没有心跳视为失效。取消后已爬页面保留，跨页面检查不执行。
- **安全。** 只连公网地址：程序自己解析域名，所有解析结果都是公网地址才连接，并按 IP 直连（防 DNS 重绑定）；拒绝 localhost、`.local`、`.internal` 和云元数据地址；不读代理环境变量；跳转不自动跟随，逐跳重新校验。起始网址或其跳转指向内网时，审计以 `target_blocked` 失败。
- **与上游的差异。** ①按网页声明的字符集解码，上游一律按 UTF-8，GBK 等编码的中文网站会乱码；②robots 规则只作用于 robots 文件所在的协议、域名和端口，与上游 robots-parser 一致，记录在案；③限流状态只在内存里，断点续跑时从头计；④每页只存前 500 个站内链接，以 JSON 存在页面行里（Supabase 连接池慢，避免每页几百条插入）；⑤不执行 JavaScript，上游可选的浏览器渲染和 Lighthouse 没有做，疑似 JS 外壳的页面会单独标出并跳过内容检查。
- **页面。** 工作台新增「技术审计」标签：填起始网址和页数上限后开始；运行中每 3 秒刷新进度，可取消；结果按严重程度列出每类问题的说明、修法和受影响网址，另有页面列表。达到页数上限、被限流、失败、取消、一页没爬到、存在 JS 外壳时，都会在结果上方写明结果不完整的原因。
- **专家。** 用户同意专家也能发起审计：`seo_start_audit` 只能审计项目自己的网站（带不带 www 都行），页数上限同样是 200，每个项目同时一个；专家发起的审计由服务器每 30 秒一次的扫描接手。查看用 `seo_list_audits`、`seo_get_audit_issues`、`seo_get_audit_pages`。问题说明在后端 i18n `seo.audit.issues.*`，接口按请求语言返回，专家拿到英文。
- **表。** 新增 `seo_audits`、`seo_audit_frontier`、`seo_audit_pages`、`seo_audit_issues`，都是新表，写在 `seo_workbench.sql` / `.pg.sql` 里，版本号仍是 20。

验证：`make all` 中格式、Ruff、mypy（580 个文件）通过，pytest 4497 通过、17 跳过、3 失败，失败的仍是本机缺 Linux `/proc` 的飞牛 NAS 测试；审计单元测试 154 项（含上游样本对照、SSRF、限流、规则、爬虫、断点续跑、取消、删除、认领失效）和 HTTP 集成测试通过；同一批 SEO 单元测试在本机临时 PostgreSQL 17 上跑，审计部分全部通过（R1 的 `test_reference_fallback_threshold` 在 PG 上失败，原因是三个供应商请求并发发出、测试却断言了顺序，与审计无关）；前端 `tsc -b`、eslint、`npm run build` 通过，SEO 相关 vitest 17 项通过。真实爬取：对 example.com 跑通一次，1 页、4 条警告，与页面实际内容相符。

### 2026-10-16 补：被机器人防护拦下，以及按密钥请求头放行

线上第一次审计（shortsbinge.com）只抓到首页：网站开着 Vercel 的机器人防护，对首页、`robots.txt`、`sitemap.xml` 一律回 429 加 `x-vercel-mitigated: challenge`。照上游的做法，429 一律算限流，爬虫退避了约 3.5 分钟才停，页面写的是「被限流」。用户定了两件事：

- **Vercel 质询算被拦下。** 429 带 `x-vercel-mitigated: challenge` 时不再退避重试，这一页记为 blocked（问题「爬虫被拦截」），审计不标限流；结果上方写明有几页被机器人防护或防火墙拦下。Cloudflare 那边照上游不变（上游注释：Cloudflare 的限流规则也会带 `cf-mitigated`）。这是与上游的第 ⑥ 处差异。
- **按密钥请求头放行。** 环境变量 `OCTOP_SEO_AUDIT_HEADERS`（JSON，网站 → 请求头）给指定网站的每个请求加上请求头，只发给这个网站和它的 www 形式，跳到别的主机时不带；网站的防火墙按这个头跳过机器人防护。值只在环境变量里，不进库、不进日志；写错的 JSON 记一条不含值的警告后忽略。每次审计开始时读一次。「爬虫被拦截」的修法也改成先推荐这种做法，按 User-Agent 放行留作备选（别人能冒充）。

验证：审计单元测试 161 项通过（新增 Vercel 质询判为拦下、整站被质询的审计不标限流且不重试、请求头只发给本站和 www、从环境变量读取、写错的配置被忽略且日志里没有值）。


### 2026-10-16 补：审计加深（按块上线：A 爬取顺序、B 规则、C 渲染、D 专家工具）

balancebiketoddler.com 的审计只报了三类小问题，同一个站 ChatGPT 查出了加载时写 noindex、死链、数据前后矛盾等。对照 nevermore、claude-seo、marketingskills 和 SEOnaut 之后，用户定了按 A → B → C → D 落地，每块单独提交，方便按块回滚。

- **A：按点击深度爬。** 队列原先按「链接发现的在前，再按入队序号」取下一页。站点地图里的网址一开始就按地图顺序入队，被链接到后序号不变，所以实际是照着站点地图的顺序爬：那次 50 页里 49 页是产品详情页，首页链出的指南、评测、尺码表一页没爬到。现在按深度取（深度相同再按序号），首页、一级页、二级页依次来。这是与上游的第 ⑦ 处差异（上游同样按入队顺序）。新增检查「可收录页面不在站点地图里」（`not-in-sitemap`，警告）：页面可收录、规范网址指向自己、有站内链接，却不在任何站点地图里；网站没有站点地图时不报。
- **B：按页面类型轮流爬，补 13 类检查。** 每个网址按路径形状归类（`/products/bikes/x` → `/products/*/*`，开头的语言段不算，顶层页各自一类），队列先取「已爬页数最少的那一类」，同类里再按链接优先、深度优先。50 页的额度会先把每类页面各看一页，再看第二页。canonical 和 hreflang 指向的网址也会入队（没有点击路径，排在链接后面），供跨页检查使用。新增检查（参考 nevermore 的检查目录和 SEOnaut 的问题清单）：站点地图里有被 robots.txt 拦给 Googlebot 的网址（发现阶段判定）；站点地图里有跳转、报错、noindex 或 canonical 指向别处的网址；软 404（返回 200、标题或 H1 写着「找不到」、正文少于 300 词）；canonical 指向报错、跳转、noindex 的页面或形成链；head 被不属于 head 的元素提前结束，之后的 canonical、robots、description、hreflang 落进 body；hreflang 代码无效、用相对网址、缺自指；hreflang 指向不可用页面、缺回链；JSON-LD 解析失败（正文超过 1 MiB 被截断时不判）；富结果类型缺必填字段（只判核对表里的 13 种类型，表外不判）；FAQ 标记里的问题在可见正文里找不到；站内链接加了 nofollow；图片没写宽高。页面表新增 `h1s`、`alternates` 两列，队列表新增 `cluster` 列，已有数据库启动时用 `ALTER TABLE ... ADD COLUMN` 补上。
- **C：无头浏览器渲染抽样页面，和 HTML 对比。** 爬完后进入新的「渲染」阶段（`phase=render`）：从爬到的正常 HTML 页面里抽样（首页在前，再按页面类型从大到小轮流取，数量为爬取上限的五分之一，最少 5 页、最多 20 页。类型大小按审计发现的同形网址数算，不按已爬页数：爬取是各类型轮流的，已爬页数看不出哪类页面最多。第一次本机实测按出现先后取，10 个名额被关于页、免责声明这类单页占满，174 个产品详情页一页没渲染到，所以改成按大小），用无头 Chromium（Playwright，手机视口）逐页打开，等网络空闲再多等 1.5 秒后读取页面。页面一开始就注入一个监听脚本，记下标题和 robots 经历过的每个状态，而不只是最后的样子。新增检查：渲染后变成 noindex（严重）；渲染过程中出现「找不到」标题或临时 noindex（严重，详情给出第几毫秒出现、最后是否还在）；渲染后 H1 变了、canonical 变了（警告）；主要内容要渲染后才出现（渲染后字数至少是 HTML 的 1.5 倍且多出 200 词以上，警告）；只在渲染后才出现的站内链接（3 个及以上，警告；HTML 被截断或链接存满时不判）；渲染过程中标题变了（最后变了，或中途换成别的标题又换回来都算，「找不到」类标题归上面的错误状态）、不带 href 的链接元素（提示）；渲染失败、无法渲染（提示）。只在渲染后出现、爬取时没抓过的站内链接，每次审计最多 30 个用同一个爬虫客户端查状态，失效的记为站内死链（详情带 `rendered_only`）。浏览器的每个请求先解析主机、确认全是公网地址才放行（DNS 重绑定是已知的残余风险），图片、媒体、字体不加载，Service Worker 禁用，每页最多 300 个请求；`OCTOP_SEO_AUDIT_HEADERS` 的密钥头只发给本站。同一进程一次只开一个浏览器；网站已经在限流时跳过渲染；启动不了浏览器时记「无法渲染」，审计照常完成。`OCTOP_SEO_AUDIT_CHROMIUM` 可指定浏览器程序。页面表新增 `body_truncated`、`render` 两列。本机用真 Chromium 打开 balancebiketoddler.com 的 4 个产品详情页：每一页都在打开后约 1.6 秒变成「Product Not Found (404)」加 `noindex,nofollow`，约 3 秒后才换回正常标题，正是 ChatGPT 报告里的 SEO-01；首页标题也在 1.7 秒到 3.7 秒之间换成另一个标题。
- **D：专家工具「检查单页」。** 新增 `seo_inspect_page(url, render=true)`：只许检查项目自己的网站（带不带 www 都算），实时抓取，不存库、不花钱。返回每一跳跳转（跳到别的网站就停，密钥请求头不跟过去）、状态码；服务器返回的 HTML 里的标题、描述、canonical、robots、H1、标题层级、hreflang、链接、图片、JSON-LD 原文及必填字段检查、正文；`render=true`（默认）时再用无头浏览器打开，给出标题和 robots 经历过的每个状态、完整标题大纲、主区域可见正文（折叠的手风琴内容不算可见），以及和审计渲染阶段同一套规则得出的 `render_findings`。另有 `content_signals`：阅读时长的说法和按每分钟 230 词估算的时长、带单位的数字（尺寸、重量、月龄年龄）、「我们测过」一类第一手测试的说法，只做英文模式的摘录、不做判断，由专家对照正文找矛盾。页面上的一切都标为网站自己的内容，只当数据分析、不当指令执行。结果超过 6 万字符时先截正文。本机对 balancebiketoddler.com 一个产品页实测：结果约 1.7 万字符，找出加载中 2.2 秒时的「Product Not Found (404)」加 noindex、HTML 与渲染后的 H1 不一致、9 个只在渲染后出现的导航链接；测量摘录里同时出现「最大承重 50 磅」和常见问题里的「重约 50 磅」，正是 ChatGPT 报告里数据前后矛盾那一类。专家的 SOUL 同步改写：调查流程、工单格式（编号、P0 到 P2、负责人、范围、证据、修法、验收标准、上线顺序），不再说爬虫不执行 JavaScript。
- **验收后修正（五处，各自单独提交）。** ①站点地图读取有上限（网址数为爬取上限的 20 倍、最少 500、最多 5 万，文档最多 300 份，索引最多嵌套三层），读到上限或有更深的嵌套没读时，审计记 `sitemap_truncated`，这时不再报「不在站点地图里」：没读到的那部分地图里可能就有这些页面。②每页 300 个请求的上限只算真正放行的请求，本来就不加载的图片、媒体、字体不再算进去；真到了上限，这一页记「渲染失败」，不再和 HTML 对比，因为页面自己的数据请求被拦了，它显示的错误状态是审计造成的。③渲染后浏览器停在了别的网址（脚本跳转，或只有浏览器遇到的服务器跳转）时，新增检查「渲染后跳到了别的网址」（`render-redirected`，警告），不再拿另一个页面和这一页的 HTML 对比，也不查那一页的链接。判断依据是当前文档加载时的网址，`history.replaceState` 只改地址栏，不算跳转。④「同一进程一次只开一个浏览器」改由打开浏览器的函数统一把关，单页检查也受它约束：审计排队等，单页检查最多等 30 秒，等不到就只返回 HTML 部分，渲染部分标为浏览器正忙。⑤浏览器崩溃后关闭页面时的报错不再往外抛，只记这一页渲染失败，审计照常完成。
- **验收后修正（第二批，十处，各自单独提交）。** ⑥「软 404」和「渲染时出现错误状态」原先在标题或 H1 里看到 404、not found、不存在、已下架就算，讲 404 错误的短文、叫 Model 404 的商品、剧名带「不存在」的剧都会被当成错误页；现在标题按 `|`、` - ` 等分隔符切开后，要有一段整段就是错误页对自己的叫法（「Page not found」「Product Not Found (404)」「页面不存在」）才算。⑦正文被截断时不再报「FAQ 结构化数据里的问题在页面上看不到」：问题可能在截断的部分里。⑧按路径归类时，开头一段只有真是语言代码才当语言段去掉：ISO 639-1 的两字母代码（不含 my），加上常被当语言段用的 us、cn、jp 等地区码，可带 zh-Hant-TW、pt_BR、es-419 这样的写法；/tv/、/my/、/my-blog/、/on-sale/ 不再被去掉。⑨hreflang 互指只从规范页出发检查，`?page=2` 这类规范指向别处的变体不再报「没有回指」。⑩JSON-LD 块不再先截到 20 万字符再解析，大块的合法标记不再被算成无效（整页最多 1 MiB）。⑪爬取因网站限流停下时，渲染阶段不再向它发请求，报告里补一条「这次没有渲染页面」（`render-unavailable`），详情写明原因，免得看起来像渲染抽样没发现问题；这条检查的文案改成两种原因都涵盖。⑫页面 20 秒内没触发 load 事件时，这一页记「渲染失败」，不再和 HTML 对比：它的数据可能还没回来。⑬页面打开的 WebSocket 也走公网地址检查，连内网、本机或 `.internal` 的直接关掉；Chromium 启动参数加 `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`，WebRTC 不能直接发 UDP。仍然拦不到的：Web Worker 里开的 WebSocket（Playwright 不路由，本机实测过）、WebRTC 经 TCP 的中继，以及两次 DNS 解析之间换了地址。⑭渲染后的 H1 和标题大纲先去掉 svg、script、style、noscript 里的文字再取，和 HTML 分析器一致：SVG logo 的 `<title>` 不再让首页报「渲染后 H1 变了」。⑮`Question` 有 `acceptedAnswer` 或 `suggestedAnswer` 之一就算齐（QAPage 的问题可以只有推荐答案）；节点是单独判断的，所以 FAQPage 里只有推荐答案的问题也不会被报。

### 2026-10-19 补：对照 nevermore 页面检查目录补 9 项

用户让把完成度盘点里「免费、不加依赖就能补」的那一批落地。逐条对照 nevermore（GenGrowth）`packages/public-tools/src/agent-audit/catalog.ts` 的页面级目录，严重程度按它的定：它的 Warning 记「警告」，Tip 记「提示」。

- **新增 9 类（都是抓取时逐页判断，不加列）：** 页面用 HTTP 提供（`http-page`，警告，对应 A8）；没有移动端 viewport（`missing-viewport`，警告，1.9）；首图被设成延迟加载（`first-image-lazy`，警告，5.4，和 nevermore 一样用文档里第一张图代替首屏图）；没有声明页面语言（`missing-html-lang`，2.7）、没有声明字符集（`missing-charset`，2.8，页面里的 meta 和响应头都算）、没有声明站点图标（`missing-favicon`，2.9）、Open Graph 标签不全（`incomplete-open-graph`，2.6）、没有结构化数据（`no-structured-data`，7.1）、没有面包屑标记（`missing-breadcrumb-schema`，7.5），这 6 类是提示。
- **口径：** viewport、lang、字符集、图标在 JavaScript 空壳页上也判断（页头在空壳里也有）。Open Graph、结构化数据、面包屑只看可收录且 canonical 指向自己的页面；正文被截断时不报后两类；JSON-LD 解析失败时只报「结构化数据无效」，不再加报「没有结构化数据」；完全没有结构化数据时只报这一条，不再加报面包屑；首页（含 `/en/` 这类语言首页）不要求面包屑。结构化数据的类型同时看 JSON-LD 和 microdata 的 `itemtype`，用 microdata 写面包屑的站不会被误报。
- **没加的：** 新窗口链接缺 `rel=noopener`（6.6）不加，现代浏览器对 `target=_blank` 默认就是 noopener，对 SEO 也没有影响；与站内其他页 70% 相似（4.5）要做相似度哈希和两两比较，Schema 类型与页面类型是否匹配（7.2）要先判断页面类型，这两项另做。

## 2026-10-19 R2 补：Search Console（GSC）

用户确认复用 GenGrowth 的 Google OAuth 客户端。移植自 OpenSEO d7ea785 的 `googleOAuth.ts`、`gscClient.ts`、`GscService.ts`、`searchAnalytics.ts`、`searchPerformanceReport.ts`，原有单元测试的用例逐条移到 `tests/unit/seo/google/test_gsc.py` 做对照。

- **授权：** 每个管理员一份 Google 授权，一次同意同时拿 Search Console 和 Analytics 的只读权限（OpenSEO 分两次问）。OAuth 客户端读 `OCTOP_SEO_GOOGLE_CLIENT_ID` / `OCTOP_SEO_GOOGLE_CLIENT_SECRET`，没设时页面显示要登记的回调地址。回调 `/api/seo/google/callback` 不带 JWT，靠一次性 state（10 分钟过期，存 PKCE verifier 和回调地址）认出是哪个管理员；返回地址只接受站内路径。令牌和 DataForSEO 凭据一样用 fernet 加密存库，过期前 60 秒刷新；再次同意时 Google 不一定再给 refresh token，就沿用旧的。ID token 直接来自 Google 令牌端点（TLS），按 OpenID Connect Core 3.1.3.7 不验签名，但核对 aud、iss、exp。两个数据权限都没勾就不存授权，让管理员回去勾。
- **绑定：** 一个项目绑一个资源，必须是所选账号下有验证权限的资源（未验证的列出但不可选）。断开账号时，经它绑定的项目一并解绑。
- **页面：** 新标签「搜索表现」：连接 / 断开账号、选资源绑定、时间范围（7 天到 16 个月，截止约 3 天前，起点不早于 16 个月前）、设备、国家、查询词和网址包含筛选；点击、展示、CTR、平均排名对比上一个等长周期；每日曲线；热门查询和热门页面（每页 25 条，多取一条判断有没有下一页）；「差一点进前排」（查询词 × 页面取前 1000 行，每个词取排名最好的页面，落在第 5–20 名的按展示量排）；国家；网址检查（Google 保存的收录状态，最多 10 个，单个网址出错不影响其他）。数据每次实时向 Google 读，免费，不存库。
- **专家工具：** `seo_gsc_performance`（任意维度和筛选；Google 只按点击排序、不能按排名筛，所以带排名或展示量条件时取窗口内前 1000 行在本地筛，翻页始终按 Google 的行号继续）和 `seo_gsc_inspect_urls`（站点地图和引用网址只给前 5 个加总数）。都只读、免费；项目没绑资源或授权失效时报错并提示重新连接。
## 2026-10-19 R2 补：GA4

移植自 OpenSEO d7ea785 的 `ga4Client.ts` 和 `features/ga4/services/*`，原有用例逐条移到 `tests/unit/seo/google/test_ga4.py`。范围按 OpenSEO 的只读部分，没做它的数据保存和定时任务。

- **授权：** 沿用 Search Console 那份 Google 授权（已含 `analytics.readonly`），不用重新同意。账号没勾 Analytics 或授权失效时，资源列表里标「需要重新连接」；Analytics Admin API 没开时标出来。
- **绑定：** 一个项目绑一个 GA4 媒体资源，必须在所选账号的 accountSummaries 里（分页读完，最多 100 页）。绑定时记下资源的时区和币种。断开账号时一并解绑。
- **日期：** 按资源时区算，最后一个完整日是那边的昨天；默认最近 28 天；结束日晚于昨天时截到昨天并给 `end_date_clamped` 提示；起止只给一个、倒序或整段都在今天以后会报错。
- **页面：** 「搜索表现」标签里加「Analytics（自然搜索流量）」卡片：选资源绑定 / 解绑；会话数、活跃用户、互动率、关键事件对比上一个等长周期（互动率按百分点）；会话数按天 / 按周曲线；关键事件比上期少一半以上（上期至少 5 个）时提示；Google 做了阈值处理、抽样、归入 (other) 或隐去指标时提示数据不全。每次实时读，免费，不存库。
- **专家工具：** `seo_ga4_overview`（同上，自然搜索）和 `seo_ga4_report`（七种报表：落地页、页面表现、关键事件、流量获取、电商、站内搜索、受众；默认只看自然搜索，站内搜索看全部渠道；单页最多 1000 行，结果太长时截断并给下一页的 offset）。都只读、免费。

## 2026-10-19 R3：竞争对手

用户定了「外链可以先不做，其他可以做，预算暂时不做限制」。这一块移植 OpenSEO d7ea785 的域名概况（`features/domain/services/DomainService.ts`、`domainKeywordMapper.ts`）和 `find_serp_competitors`（`mcp/tools/dataforseo-research-tools.ts`、`lib/dataforseo/labs.ts`），不含外链。

- **两种查询：** 「域名概况」买 DataForSEO Labs `domain_rank_overview`（只取自然流量估算、排名词数和前 1 / 2-3 / 4-10 名的词数，外链和引用域名不买）加 `ranked_keywords`（按流量取前 100 个自然排名词，同时记下总词数）；「谁在跟你抢这些词」买 `serp_competitors`（1-200 个关键词，取前 50 个域名，只看自然结果，按可见度排）。域名按项目背景里竞争对手的规则统一成不带 www 的主机名；关键词去重、转小写后排序，同一组词不管顺序和大小写都算同一个查询。
- **购买和复用：** 和 SERP 一样，只在页面上手动确认后购买，不重试；同一请求标识重放原记录，换了目标就报冲突；同一目标和市场 12 小时内有非空的成功记录就直接复用、不收费，失败或空结果不复用。域名概况是两次调用，第二次失败时保留第一次的数据和费用；费用和 research 一样分「总费用」（有未知就为空）和「已知费用」。超过 5 分钟仍在执行的记录标为 unknown。
- **页面：** 新标签「竞争对手」。域名框可以选自己的网站或项目背景里记过的竞争对手；结果表可按流量、排名、搜索量排序，链接只放 http(s)。抢词卡片可以一键填入搜索量最高的 20 个已保存关键词；结果里标出自己的网站和已记录的竞争对手，其余可以一键「记为竞争对手」写进项目背景。
- **专家工具：** `seo_list_competitor_lookups` / `seo_get_competitor_lookup`，只读、免费，买不了新的（和 SERP 同一个规矩）。

表 `seo_lookups` 追加在 `seo_workbench.sql` / `.pg.sql`（`CREATE TABLE IF NOT EXISTS`），版本号不变。

### 2026-10-19 R3 补：排名追踪

移植 OpenSEO d7ea785 的排名追踪（`features/rank-tracking/services/RankTrackingService.ts`、`rankTrackingResults.ts`、`lib/dataforseo/serp.ts` 的 `fetchRankCheckSerp`、`shared/rank-tracking.ts` 的费用估算），只做手动查询；定时查询（上游走 DataForSEO 任务队列，便宜约七成）没做。

- **追踪：** 一个追踪 = 一个域名（默认项目自己的，子域名算同一个站）在项目市场里的一组关键词，选设备（桌面和移动 / 只桌面 / 只移动）和查询深度（10-100，以 10 为单位）。建追踪和加减关键词都免费；一个项目最多 20 个追踪，一个追踪最多 200 个词（上游是 500 / 1000，这里先收紧）。
- **查询：** 每个关键词、每种设备买一次实时 Google 自然结果（`serp/google/organic/live/advanced`），带 `stop_crawl_on_match`（只在自然结果里找，含子域名），找到域名所在那一页就停，只按爬过的页数计费。排名取 `rank_group`（自然结果里的名次，和人眼里的排名一致），记下排名网址和这页结果里出现过的类型。后台同时跑 5 个，每查完一个就写一条快照、更新进度；页面每 3 秒刷新一次。一个追踪同一时间只能有一次查询；没有重试。
- **费用：** 估算照上游实时价：第一页 $0.002，之后每页 $0.0015，关键词里有 `site:` 一类高级搜索语法按 5 倍算。页面先显示「最多 $X」，确认时把这个数一起交上去；服务器算出的估算比它高（期间加了词或改了设置）就拒绝，什么都不买。实际费用按每次返回的金额累加，有一次没拿到回复就记成未知。
- **中断：** 每查完一个更新一次心跳；超过 3 分钟没心跳（重启）就把这次查询记为 unknown / interrupted、费用未知，并放开这个追踪。已经写下的快照照常算数。
- **结果：** 每个关键词每种设备取最新一次快照，和「对比期之前最新的一次」比较（1 / 7 / 30 / 90 天，默认 7 天），没有那么早的就和第一次比，和上游一样。移除的关键词不再显示，历史还在。
- **专家工具：** `seo_list_rank_trackers`、`seo_get_rank_results`、`seo_get_rank_history`，只读、免费，不能发起查询。

表 `seo_rank_trackers`、`seo_rank_keywords`、`seo_rank_runs`、`seo_rank_snapshots` 追加在 `seo_workbench.sql` / `.pg.sql`，版本号不变。

## 2026-10-19 R4：AI 可见度（提问追踪）

移植 OpenSEO d7ea785 的 Prompt Tracking（`features/ai-visibility/services/aiVisibilityCollection.ts`、`aiVisibilityMatching.ts`、`aiVisibilityResults.ts`、`shared/ai-visibility-results.ts`、`providers/dataforseoEvidence.ts`、`lib/dataforseo/ai-tracking.ts`），只做手动查询。没做的：Claude 和 Perplexity（上游走按 token 计价的 LLM Responses 接口，解析也不同）、定时查询、Prompt Explorer（一条提问的追踪就能代替）、Prompt Research（上游靠 OpenRouter 生成，我们没有）、趋势图。

- **提问和引擎：** 每个项目一组提问（最多 100 条，带主题，默认 General），引擎在 ChatGPT、Gemini、Google AI 概览里选（默认前两个），市场就是项目市场。加减提问、换引擎都免费。
- **品牌：** 自己的品牌是项目名加域名（去掉 www），竞品取项目背景里保存的竞争对手。每次查询开始时把品牌名单存进这次记录，之后改竞品不会改旧结果。
- **查询：** 每条提问、每个引擎买一次实时回答：ChatGPT、Gemini 走 DataForSEO LLM Scraper（`ai_optimization/{chat_gpt,gemini}/llm_scraper/live/advanced`），AI 概览走实时 Google 自然结果（`serp/google/organic/live/advanced`，`depth: 10`、`load_async_ai_overview: true`，没加载出概览的那部分会退款）。提问里的 `%`、`+` 先转义（DataForSEO 会解码）。单次请求最长等 150 秒；后台同时跑 5 个，每拿到一个就写一条回答、更新进度；页面每 3 秒刷新。一个项目同一时间只能有一次查询；没有重试。
- **费用：** 照上游实时价，每条回答 $0.004。确认时把显示的金额交上去，服务器算出的比它高就拒绝。实际费用按每次返回的金额累加，有一次没拿到回复就记成未知。
- **解析：** 回答正文去掉图片和引用角标（`[1]`、`[域名]`）、markdown 标记后用来找品牌；引用只收 http(s) 且不带账号密码的链接，去掉 # 和 utm_* / gclid / fbclid / msclkid，按网址去重编号。AI 概览取结果里 `type: ai_overview` 那一项的正文，引用取它自己和各段落的 `references`。没有正文就记成「没有 AI 回答」（AI 概览常见）。
- **匹配：** 品牌名或域名作为整词出现在正文里算「被提到」（Unicode 规范化后比，正文里的网址和邮箱不算）；名字就是域名的品牌（如 rival.com）也认去掉后缀的词（rival），后缀只认项目四个市场常见的两段式（co.uk、com.au 等），没有引入公共后缀表。引用里有该品牌域名（含子域名）的网页算「被引用」。提问本身提到自己品牌或域名的算「含品牌提问」，默认不计入结果。
- **结果：** 默认看最新一次查询（可选最近 20 次里的任意一次）。品牌汇总只数「有回答」的条目：被提到次数、被引用次数、平均位次（按每条回答里各品牌第一次出现的先后排）；失败和「没有 AI 回答」都不算没提到。另有被引用最多的 50 个域名（标出自己的和竞品的）、「只看提到竞品没提到你」的筛选，以及每条回答的原文和引用列表。回答原文是模型输出，页面只按纯文本显示。
- **中断：** 和排名追踪一样，6 分钟没心跳（重启）就记为 unknown / interrupted、费用未知；已写下的回答照常算数。
- **专家工具：** `seo_list_ai_prompts`、`seo_get_ai_visibility`（rows / brands / sources 三个视图分页）、`seo_get_ai_answer`（正文前 24000 字和引用），只读、免费，不能发起查询。

表 `seo_ai_settings`、`seo_ai_prompts`、`seo_ai_runs`、`seo_ai_answers` 追加在 `seo_workbench.sql` / `.pg.sql`，版本号不变。

## 2026-10-23 R5：补齐 OpenSEO 的另外四块

用户看了「虾仁 SEO」的抖音视频（seo.fuckseo.io），想把它集成进工作台。拆它公开的前端包后确认：它就是上游 OpenSEO（约 10-19/20 的版本）加一层页面文字替换式的中文、AI 改用 DeepSeek、用户自己填 Key，没有自研功能。所以这一轮不接它的网站，而是从上游 OpenSEO 2d8236a 原生移植我们还缺的四块。用户定了「四块都做：页面维度、外链、提示词探索、品牌查询，按顺序一块一提交。付费都在页面上点」。四块都存在 `seo_lookups` 里（新的 kind），不加新表；规矩和 R3 一样：只在页面上手动确认后购买，不重试，同一请求标识重放原记录，专家工具只读。

### ① 热门页面（kind `pages`）

移植 `features/domain/services/domainPagesPage.ts`：买 DataForSEO Labs `relevant_pages`，按自然流量估算（`metrics.organic.etv`）倒序取前 100 个页面，记下总页数。每行是网址、估算流量、排名词数和排在第 1 / 2-3 / 4-10 名的词数；没有网址的行丢掉，缺的数保持未知、不当 0。上游按页翻、带包含 / 排除词和区间筛选，这里一次买前 100 个，在页面上排序。

- **购买和复用：** 和域名概况同一套：同一个域名和市场 12 小时内有非空的成功记录就直接复用、不收费；和同一个域名的「域名概况」是两次独立购买。约 $0.012 一次加每行 $0.00012，100 行约 $0.024。
- **页面：** 「竞争对手」标签里在「域名概况」下面加「热门页面」卡片，域名框同样可选自己的网站和项目背景里的竞争对手；链接只放 http(s)。前 3 / 前 10 的词数由 1 / 2-3 / 4-10 相加，有一项未知就显示未知。
- **专家工具：** 沿用 `seo_list_competitor_lookups` / `seo_get_competitor_lookup`（kind 多了 `pages`）。

### ② 外链（kind `backlinks`）

移植 `features/backlinks/`（`server/backlinks.ts`、`services/backlinksPage.ts`）：一次购买同时调用 DataForSEO Backlinks 的 5 个接口，结果存成一条记录。

- **概况 `summary/live`：** 外链数、引用页面、引用域名、失效外链和页面、权威分（0-100）、外链垃圾分和目标垃圾分、新增和丢失外链、新增和丢失引用域名。
- **趋势 `history/live`：** 从昨天（UTC）往前一年，每月一行，按日期从早到晚，没有日期的行丢掉。
- **最强外链 `backlinks/live`：** 每个来源域名取一条（`one_per_domain`），按外链权威分倒序取前 100，垃圾分 40 及以上的不要（没有垃圾分的保留）。
- **引用域名 `referring_domains/live`：** 外链垃圾分不超过 40，按外链数倒序取前 100。
- **被链接最多的页面 `domain_pages_summary/live`：** 按外链数倒序取前 100。

五个调用共用上游的参数：含子域名、含间接链接、排除站内链接、只看现存外链、权威分用 0-100 刻度。外链数据不分市场，记录上照样记项目市场，只用来定复用范围。

- **失败：** 五个调用并发，各自记花费和回执。有一个失败，整条记为失败（或结果未知），已回来的部分照样存下、照样计费，不重试；失败的记录不会被 12 小时复用。
- **购买和复用：** 同一个域名 12 小时内有非空的成功记录就直接复用、不收费。每个调用约 $0.02 加每行 $0.00003，一次约 $0.11-0.13，确认框里写明。
- **页面：** 「竞争对手」标签里在「热门页面」下面加「外链」卡片：上面是概况数字和外链 / 引用域名的双轴趋势图，下面三个分页签分别放最强外链、引用域名和被链接最多的页面；nofollow 外链加标记，链接只放 http(s)。上游的单页明细、锚文本分布和翻页筛选这里不做。
- **专家工具：** 沿用 `seo_list_competitor_lookups` / `seo_get_competitor_lookup`（kind 多了 `backlinks`），每行带 `section`：`backlink` / `referring_domain` / `page` / `history`。

### ③ 提示词探索（kind `prompt`）

移植 `features/ai-search/services/promptExplorer.ts`、`lib/dataforseo/llm-models.ts` 和 `lib/dataforseo/ai.ts` 的 `fetchLlmResponse`：一个问题（≤500 字）同时问 1-4 个模型（ChatGPT、Claude、Gemini、Perplexity），用 DataForSEO `ai_optimization/{model}/llm_responses/live`，并排显示回答、引用的网页和模型搜过的词。和 R4 的 AI 可见度不同：那边用 LLM scraper 批量跑项目里存的提示词，这里是临时问一个问题、能挑模型和搜索国家。

- **选型号：** 先读免费的型号目录 `GET ai_optimization/{model}/llm_responses/models`。目录列着固定型号就用它（ChatGPT `gpt-5.6-luna`、Claude `claude-sonnet-5`），否则按上游规则取最高的不带后缀的主型号（`gpt-X`、`claude-sonnet-X`、`gemini-X-pro`、`sonar-reasoning-pro`），再不行取目录里有的备用型号。目录读不到时用上游 2026-08-25 的备用列表。最后选出的名字不在目录里就不发送：DataForSEO 对写错型号的任务也收费。上游把目录在进程里缓存 1 小时，这里每次购买现读（免费，点得少）。
- **请求：** `user_prompt` 原样发；`max_output_tokens` 4096（推理模型的隐藏思考也占这个额度）；联网时只有 Claude 加 `force_web_search`（ChatGPT 会拒）；国家只在联网时发。Gemini 不收国家，Claude 只收 36 个国家（列表照上游）；不支持的组合不发送、不收费，那一行记 `unsupported_country`。页面上的国家下拉只给这 36 个国家（上游给全部 249 个）。
- **结果：** 每个模型一行：型号名、状态、回答正文（`message` 段落用空行拼接）、引用（只收 http(s)、去重、最多 25 条、网址超过 2048 字符的丢掉）、模型搜过的词（最多 20 个）、输出 token、是否真的联网、这一行的费用。
- **失败和复用：** 各模型并发；一个模型失败不影响其他模型，失败那行照记费用和回执。和外链、品牌查询一样，有一个模型的请求失败整条就记为失败（有结果未知的记为未知），保留其他模型的回答但不复用，再点一次就全部重买（2026-10-23 用户在验收时定的）；国家不支持、型号没列出这类没发出去、不收费的拒绝不算失败。同一个问题（只合并空白、保留大小写）、同一组模型、联网开关和国家 12 小时内有完成的记录就直接复用、不收费。上游按「问题 + 型号」各缓存 7 天，重试只补失败的模型；这里按整次提问缓存 12 小时，和其他查询一致。上游在模型没联网就作答时会自动再花钱问一次，这里不重试。
- **费用：** 确认框按上游的预留价估最多多少钱：联网时 ChatGPT $0.09、Claude $0.18、Gemini $0.11、Perplexity $0.04，不联网时 $0.05 / $0.08 / $0.05 / $0.04。实际按 DataForSEO 回执记账。
- **品牌高亮：** 不进请求，也不存。页面上按上游规则比对：引用的网址或标题包含品牌，或正文按词边界提到品牌，就标「提到了」。默认填项目域名的第一段，改了不用重新付费。
- **页面：** 「AI 可见度」标签最下面加「提示词探索」卡片；回答只按纯文本显示，链接只放 http(s)。
- **专家工具：** 新增只读的 `seo_list_ai_lookups` / `seo_get_ai_lookup`；后者一次读一个模型的回答（正文前 24000 字），引用分页。竞品查询的列表和工具都不会列出提问记录，反过来也一样。
- **HTTP：** `POST /seo/projects/{id}/ai-lookups/prompt`、`GET /seo/projects/{id}/ai-lookups`、`GET /seo/projects/{id}/ai-lookups/{lookup_id}`。等待中的提问超过 6 分钟没结束记为中断（竞品查询还是 5 分钟）。

### ④ 品牌查询（kind `brand`）

移植 `features/ai-search/services/` 里的 `brandLookupShaping.ts`、`citedSources.ts`、`shareOfVoice.ts` 和对应的 LLM Mentions 调用：看一个品牌在 ChatGPT 和 Google AI 概览的回答里被提到多少次、这些问题的 AI 搜索量、提到它的回答最常引用哪些网页，以及具体是哪些问题；可以带最多 5 个竞品比被提到次数的占比。

- **目标：** 一个不带空格、带点的词当域名（去掉协议、路径和 `www.`，连子域名一起算），其他当品牌名（按整词匹配回答正文和品牌实体）。竞品最多 5 个，忽略大小写去重，和目标相同的去掉。
- **请求：** DataForSEO `ai_optimization/llm_mentions/*`，两个平台（`chat_gpt`、`google`）各 3 个接口：`aggregated_metrics`（被提到次数和 AI 搜索量）、`top_pages`（`links_scope: sources`，提到它的回答引用最多的网页）、`search`（提到它的问题，取 100 条）；填了竞品再各调 1 次 `cross_aggregated_metrics`。ChatGPT 只有美国英语数据，它的调用固定用美国英语，Google 用项目市场；项目市场不是美国英语时份额不算 ChatGPT，它的 `cross_aggregated_metrics` 就不买（上游两个平台都买，再自己注明换了市场要排除 ChatGPT）。
- **结果：** 每个平台最多 25 个问题（按 AI 搜索量）和 10 个被引用的网页；每个网页附上抽样里引用它的问题（最多 50 个），每个问题附上引用的网页（最多 10 个，只收 http(s)）和回答里提到的品牌（最多 20 个）。概况是被提到次数和 AI 搜索量的合计、每个平台的数、近 12 个月的 AI 搜索量（抽样问题的月度搜索量相加）和被提到次数占比。项目市场不是美国英语时，ChatGPT 单独列出，不算进合计、趋势和占比（同上游）。占比只算请求的品牌，接口多给的键不算；没数据的品牌是未知，不当 0。
- **失败：** 全部调用并发（上游按顺序调，是为了它托管版的余额检查）。任何一个失败整条就记失败或未知，已回来的部分照存、照计费，不重试，也不被复用。
- **购买和复用：** 同一个目标和同一组竞品（忽略大小写和顺序）12 小时内有非空的成功记录就直接复用、不收费；上游缓存 24 小时，这里和其他查询一样用 12 小时。每次调用 $0.1 加每行 $0.001，不带竞品约 $0.81，带竞品约 $1.02，确认框里写明。比最早估的 $0.3-0.5 高，主要是 `search` 取 100 条。
- **页面：** 「AI 可见度」标签最下面加「品牌查询」卡片：目标默认填项目域名，竞品回车添加；上面是合计和各平台数字、趋势图、占比表，下面两个分页签放被引用的网页（展开看引用它的问题）和提到它的问题（展开看引用的网页和提到的品牌）；链接只放 http(s)。上游的网址范围（只看某个路径）不做，只查整个域名或品牌名。
- **专家工具：** 沿用 `seo_list_ai_lookups` / `seo_get_ai_lookup`（kind 多了 `brand`）；品牌记录的行分页读，每行带 `section`：`page` / `query`。
- **HTTP：** `POST /seo/projects/{id}/ai-lookups/brand`；列表和单条读取和提示词探索共用。

## 2026-10-23 R6：对话和工作台打通

用户：「这套 SEO 工作台内置的功能，应该可以通过外面的对话（SEO 专家）拉起和使用，打通 SEO 工作台和 SEO Agent。」定下来的规矩（2026-10-23 用户选的）：免费的操作专家在对话里直接做；花钱的由专家填好参数，对话里出一张确认卡，用户点「确认购买」才扣钱，和页面按钮走同一个接口、同一份预估；反过来，工作台上的结果也能一键交给专家接着分析。一块一个提交。

### ① 专家能直接做的免费操作

- **新工具：** `seo_save_keywords`（把研究记录里的词存进已保存关键词，标签可追加或替换）、`seo_add_ai_prompts` / `seo_remove_ai_prompt`、`seo_set_ai_engines`、`seo_create_rank_tracker` / `seo_update_rank_tracker`、`seo_add_rank_keywords` / `seo_remove_rank_keyword`、`seo_cancel_audit`。每个都调和页面接口相同的服务方法，校验、上限（每个项目 100 条提问、20 个排名追踪，每个追踪 200 个词）和去重都一样，写进数据所有者（管理员）的项目。
- **不给专家的：** 删排名追踪、删审计、删报告会连带删掉数据，留在页面上点；移除一条提问或一个追踪词不删历史，所以给了。建项目、改绑专家、填 DataForSEO 账号、连 Google 也留在页面上。
