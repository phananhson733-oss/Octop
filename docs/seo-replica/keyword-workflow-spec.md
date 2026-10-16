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
- **C：无头浏览器渲染抽样页面，和 HTML 对比。** 爬完后进入新的「渲染」阶段（`phase=render`）：从爬到的正常 HTML 页面里抽样（首页在前，再按页面类型轮流取，数量为爬取上限的五分之一，最少 5 页、最多 20 页），用无头 Chromium（Playwright，手机视口）逐页打开，等网络空闲再多等 1.5 秒后读取页面。页面一开始就注入一个监听脚本，记下标题和 robots 经历过的每个状态，而不只是最后的样子。新增检查：渲染后变成 noindex（严重）；渲染过程中出现「找不到」标题或临时 noindex（严重，详情给出第几毫秒出现、最后是否还在）；渲染后 H1 变了、canonical 变了（警告）；主要内容要渲染后才出现（渲染后字数至少是 HTML 的 1.5 倍且多出 200 词以上，警告）；只在渲染后才出现的站内链接（3 个及以上，警告；HTML 被截断或链接存满时不判）；渲染过程中标题变了（最后变了，或中途换成别的标题又换回来都算，「找不到」类标题归上面的错误状态）、不带 href 的链接元素（提示）；渲染失败、无法渲染（提示）。只在渲染后出现、爬取时没抓过的站内链接，每次审计最多 30 个用同一个爬虫客户端查状态，失效的记为站内死链（详情带 `rendered_only`）。浏览器的每个请求先解析主机、确认全是公网地址才放行（DNS 重绑定是已知的残余风险），图片、媒体、字体不加载，Service Worker 禁用，每页最多 300 个请求；`OCTOP_SEO_AUDIT_HEADERS` 的密钥头只发给本站。同一进程一次只开一个浏览器；网站已经在限流时跳过渲染；启动不了浏览器时记「无法渲染」，审计照常完成。`OCTOP_SEO_AUDIT_CHROMIUM` 可指定浏览器程序。页面表新增 `body_truncated`、`render` 两列。本机用真 Chromium 打开 balancebiketoddler.com 的 4 个产品详情页：每一页都在打开后约 1.6 秒变成「Product Not Found (404)」加 `noindex,nofollow`，约 3 秒后才换回正常标题，正是 ChatGPT 报告里的 SEO-01；首页标题也在 1.7 秒到 3.7 秒之间换成另一个标题。
