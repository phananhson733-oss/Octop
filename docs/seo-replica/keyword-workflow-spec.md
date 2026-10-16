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
