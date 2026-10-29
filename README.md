# 智联招聘求职助手（Zhaopin Job Assistant）

基于浏览器扩展桥（Kimi WebBridge）的智联招聘自动化求职工具：**岗位列表读取 → JD 详情读取 → 规则语义判定 → 投递预检 → 确认后投递**。

架构参考自 [taohuajianxian/boss-zhipin-assistant](https://github.com/taohuajianxian/boss-zhipin-assistant)（BOSS 直聘版），本项目将其设计思路适配到智联招聘，智联侧的页面读取、规则判断与投递执行逻辑全部重写。

## 功能特性

- **分批懒加载读取**：`zl_recommend_loader.py` 自动滚动列表页分批抓取岗位卡片（标题/薪资/公司/地点/标签），支持断点续跑（`zl_batch_state.json` 即进度文件，原子落盘）
- **JD 详情读取**：`zl_jd_reader.py` 在列表页原地点击卡片，解析右侧详情面板（正文/经验/学历/招聘者），校验面板已切换到目标岗位后才读字段
- **规则语义判定**：`zl_judge.py` 基于 `user_profile.json` 的方向词/必备词/排除词打分（100 分制），输出 APPROVE / DOWNGRADE / REJECT 三档；支持标题/正文分级降权、floor 保护、日薪岗策略
- **投递预检（只读）**：`zl_deliver.py precheck` 点卡读 JD、扫描投递按钮状态、复检经验年限，不点任何投递按钮
- **投递执行（双重确认）**：`deliver` 必须显式 `--yes`，且每条投递前做实时按钮复检（闸门 2）；异常一律跳过不硬点
- **批量预检/投递**：`zl_precheck_batch.py` / `zl_deliver_batch.py` 按（标题+公司）双键匹配 DOM 位置 + 面板真实 jobId 终验，杜绝错位投递
- **流水线投递（推荐）**：`zl_stream_deliver.py` 按 DOM 顺序逐张点开 —— 读面板 → 判断 → 合规当场投，不预扫全表；决策只依据面板实时内容，列表重排也不影响正确性，且复用面板数据免去二次读取（单条约 10-12s）

## 安全机制

| 机制 | 说明 |
|------|------|
| 投递确认闸门 | `deliver()` 必须 `confirm=True`（CLI 需 `--yes`），预检永远只读 |
| 闸门 2 实时复检 | 每条投递前重新扫描按钮状态：已投递/无按钮/异常 → 跳过 |
| 验证码即停 | 投递前 + 点击后双重检测验证码/风控弹窗，命中立即中止剩余队列等人工处理 |
| 投递间隔随机化 | 条与条之间随机 3-5 秒 |
| 日投递上限 | 当日成功条数达上限（`daily_deliver_limit`，默认 80）后拒绝继续 |
| 页面守卫 | 批量任务附加的标签页 URL 必须匹配配置关键词，不符则导回或退出，绝不静默换页 |
| 台账可追溯 | 每次预检/投递/验证的按钮状态、弹窗全文、URL 全部落盘 `zl_deliver_log.json` |

## 目录结构

```
├── scripts/
│   ├── profile_loader.py      # 中央配置：user_profile.json → SESSION/城市/关键词/URL
│   ├── zhaopin_client.py      # 传输层（WebBridge HTTP）+ 全部 CSS 选择器集中配置
│   ├── surface_cache.py       # 卡片缓存：字段白名单/去重/page_order
│   ├── zl_recommend_loader.py # 分批懒加载读取列表
│   ├── zl_jd_reader.py        # JD 面板读取（原地点卡 + 面板校验）
│   ├── zl_judge.py            # 规则语义判定（支持断点续跑）
│   ├── zl_deliver.py          # 投递预检（只读）/ 投递执行（需确认）
│   ├── zl_precheck_batch.py   # 批量预检（双键匹配 + jobId 终验）
│   ├── zl_deliver_batch.py    # 批量投递（投前逐条终验）
│   ├── zl_stream_deliver.py   # 流水线投递（读一条 → 判断 → 合规即投）
│   ├── zl_judge_drive.py      # 判定的批量驱动（长等待滚表 + 断点续跑）
│   ├── zl_prescreen.py        # 候选粗筛（离线过滤，免点卡）
│   └── zl_collect_all.py      # 多关键词采集
└── tests/
    ├── selftest.py            # 判定逻辑离线用例
    ├── test_stream.py         # 流水线投递分支路由用例
    ├── domtest.js             # DOM 解析离线用例（jsdom）
    └── _js_dump/              # domtest 用的 JS 片段夹具
```

## 快速开始

1. **前置条件**：Chrome 系浏览器安装 Kimi WebBridge 扩展（本地服务端口 `10086`），并已登录智联招聘。
2. **配置**：复制 `user_profile.example.json` 为 `user_profile.json`，按注释填入你的城市编码（`city_code`）、搜索关键词、方向词/必备词/排除词等。
3. **读取列表**：
   ```bash
   python scripts/zl_recommend_loader.py --step next-batch   # 分批滚动抓取
   python scripts/zl_recommend_loader.py --step probe        # 改版后探测 DOM 结构
   ```
4. **读取 JD + 判定**：`zl_jd_reader.py` 支持传 jobId 或 idxN；`zl_judge.py` 支持全量判定与断点续跑。
5. **预检与投递**：
   ```bash
   python scripts/zl_deliver.py --precheck idx91 idx13        # 只读预检
   python scripts/zl_precheck_batch.py 10 80                  # 批量预检凑 10 条干净候选
   python scripts/zl_deliver.py --deliver idx91 --yes         # 单条投递（须显式 --yes）
   ```
6. **流水线投递（推荐日常使用）**：
   ```bash
   python scripts/zl_stream_deliver.py --max-scan 60 --target 5 --dry-run   # 只判断（演练）
   python scripts/zl_stream_deliver.py --max-scan 60 --target 5 --yes       # 真投
   python scripts/zl_stream_deliver.py --resume --yes                       # 断点续扫
   ```

## 配置说明

所有个性化配置集中在 `user_profile.json`（不随仓库分发，示例见 `user_profile.example.json`），每个字段带 `_comment_*` 说明键：城市编码、搜索关键词、底薪下限、日薪岗策略（`daily_wage_policy`）、方向词（`job_directions`）、必备/排除关键词、标题/正文分级降权（`reject_title_weight` / `reject_body_weight`）、外包/供应商公司模式等。

## 免责声明

**使用限制与风险提示：本项目仅在作者个人环境下测试通过，强依赖 Kimi WebBridge、已登录的智联账号和本地配置，不保证在他人环境下可运行。自动化投递存在平台风控风险，请自行控制投递频率、遵守平台规则。作者不对账号受限、误投等后果负责。仅供学习参考**

## 致谢 / References

- [taohuajianxian/boss-zhipin-assistant](https://github.com/taohuajianxian/boss-zhipin-assistant)：整体架构参考（Agent 驱动 + Kimi WebBridge 控制浏览器 + `user_profile.json` 配置驱动）。原项目面向 BOSS 直聘，本项目适配到智联招聘，重写了智联侧的页面读取、规则判断和投递执行逻辑。只参考设计思路，没有直接复制代码。
- [ultraplan-bit/boss-auto-apply](https://github.com/ultraplan-bit/boss-auto-apply)（MIT License）：多平台投递思路和部分安全策略（投递间隔随机化、日上限保护、失败跳过不硬点）参考了它的实现方式。没有直接复制代码，但在设计投递节奏和异常处理时借鉴了思路。

## License

[MIT](./LICENSE)
