# TrendRadar — 信息聚合渠道管理指南

本项目的代码库中，**信息聚合面板（fenix-desktop）的"渠道"全部由 TrendRadar 服务端生成**，
通过 `aggregation.storage_push` 写入 COS 的 `trendradar/push/latest.json`，面板轮询读取后
为每个渠道生成一个 Tab。因此，**新增 / 修改 / 删除渠道只需要改 TrendRadar 的配置与代码**，
面板侧无需改动（面板只做通用渲染）。

渠道分三类：

| 类型 | 配置位置 | 是否写代码 |
|------|----------|-----------|
| 热榜渠道 | `config.yaml → aggregation.storage_push.platform_channels` | 否 |
| RSS 渠道 | `config.yaml → aggregation.storage_push.rss_channels` | 否 |
| 扩展渠道（水木/InfoQ/邮件） | `config.yaml → aggregation.storage_push.extended_channels` + `trendradar/crawler/channels.py` | 是 |

---

## 一、RSS 渠道（最常用，纯配置即可）

每个 RSS 订阅源就是一个独立渠道 Tab。**只需要改配置文件，不用写代码。**

### 新增一个 RSS 渠道

编辑 `config/config.yaml`，在 `aggregation.storage_push.rss_channels` 列表末尾追加一项：

```yaml
aggregation:
  storage_push:
    rss_channels:
      - id: "sspai"                      # 唯一标识（勿与其它渠道重复）
        name: "少数派"                    # 面板 Tab 显示名称
        url: "https://sspai.com/feed"     # 订阅地址
        # max_items: 20                  # 可选：该渠道最多展示条数（默认 20）
```

### 修改一个 RSS 渠道

直接改对应项的 `name`（显示名）或 `url`（订阅地址）即可，`id` 建议保持不变。

### 删除一个 RSS 渠道

删除 `rss_channels` 中对应的整段列表项。

### 说明

- 支持 **RSS 2.0 / Atom / JSON Feed** 三种格式（内部由 `RSSParser`/feedparser 解析）。
- 条目自动带上 `published_at`（发布时间）与 `summary`（摘要）；无时间的条目由统一逻辑兜底为抓取时间。
- 若 AI 已配置（`ai` 段），英文标题会自动翻译为中文（见 `translate_titles_to_chinese`）。
- 抓取失败不影响其他渠道，该渠道 `status=error` 仍会出现在面板中（可正常查看 Tab，只是无数据）。
- 某些站点对数据中心 IP 有访问限制（如 v2ex 需要代理），实际是否可抓取取决于部署环境的网络。

---

## 二、热榜渠道（纯配置即可）

把热榜平台作为渠道发布到面板。**只改配置，不用写代码。**

编辑 `config/config.yaml`：

```yaml
aggregation:
  storage_push:
    platform_channels: ["zhihu", "weibo"]   # 平台 id 列表（见 platforms.sources 中的 id）
```

每个平台 id 会生成一个 `hotlist-<id>` 渠道 Tab（如 `hotlist-zhihu` → 「知乎热榜」）。

### 相关代码位置

- 平台渠道构建逻辑：`trendradar/__main__.py` → `_publish_to_storage()`
- 平台 id/名称定义：`config/config.yaml → platforms.sources`

---

## 三、扩展渠道（需改代码）

扩展渠道是服务端轻量数据源（无浏览器依赖），目前有：
`smth_daily`（水木每日十大）、`infoq`（InfoQ 技术热点）、`email`（电子邮件）。

### 开关（配置）

```yaml
aggregation:
  storage_push:
    extended_channels:
      smth_daily: true
      infoq: true
      email: true
      summary: false      # 是否给所有渠道条目生成中文摘要（默认关闭，耗 token）
```

### 新增一个扩展渠道（三步）

1. **写抓取函数**：在 `trendradar/crawler/channels.py` 中新增抓取函数，返回统一结构：
   ```python
   def fetch_xxx_hot(max_items: int = 20) -> Dict:
       """返回: {"ok": bool, "items": List[Dict], "fetched_at": str, "error": str}
       item 字段: {"title", "url", "published_at"(ISO), "category", "extra"}"""
   ```
   - 抓取用 `requests`，超时 `TIMEOUT`，UA 用模块级 `HEADERS`；
   - 必须 `try/except` 兜底，失败返回 `{"ok": False, "items": [], ...}`，不影响其他渠道；
   - `published_at` 统一为 ISO 8601 字符串（`fetch_smth_daily_top` 有 RFC822→ISO 转换示例）。

2. **加开关配置**：在 `config/config.yaml → aggregation.storage_push.extended_channels` 加一项，
   并在 `trendradar/core/loader.py → _load_aggregation_config()` 的 `EXTENDED_CHANNELS` 中读取它。

3. **发布为渠道**：在 `trendradar/__main__.py → _publish_to_storage()` 中，
   参照 `if ext.get("INFOQ")` 的写法，把结果 append 进 `channels` 列表：
   ```python
   if ext.get("MY_CHANNEL"):
       from trendradar.crawler.channels import fetch_xxx_hot
       result = fetch_xxx_hot(max_items=20)
       channels.append({
           "id": "my-channel",
           "name": "我的渠道",
           "fetched_at": result["fetched_at"],
           "status": "ok" if result["ok"] else "error",
           "error": result.get("error", ""),
           "items": result["items"],
       })
   ```

### 删除一个扩展渠道

- 从 `config.yaml` 的 `extended_channels` 删除对应开关；
- 从 `loader.py` 的 `EXTENDED_CHANNELS` 删除对应读取项；
- 从 `__main__.py` 的 `_publish_to_storage()` 删除对应 `if` 块；
- 可选：删除 `channels.py` 中不再使用的抓取函数。

---

## 四、渠道数据结构（面板契约）

每个渠道写入 `latest.json` 的 `channels` 数组，结构如下（面板按此渲染）：

```json
{
  "id": "sspai",
  "name": "少数派",
  "fetched_at": "2026-08-21T00:00:00+08:00",
  "status": "ok",                 // ok | error
  "error": "",
  "items": [
    {
      "title": "文章标题",         // 必现
      "url": "https://...",        // 必现，点击新窗口打开
      "published_at": "2026-08-20T23:00:00+08:00",  // ISO 8601，可选
      "category": "",               // 可选
      "summary": "中文摘要",        // 可选（summary 开关开启时有）
      "extra": {}                   // 渠道自定义扩展，面板不解析
    }
  ]
}
```

面板契约类型定义：`fenix-desktop/src/panels/aggregation/types.ts`。

---

## 五、渠道改动后的发布与验证流程（必做）

> ⚠️ **新增 / 修改渠道后，必须实际推送并触发 GitHub 工作流执行，并监控执行成功，
> 渠道才会真正生效。** 只改本地代码/配置不算完成。

### 第 1 步：提交并推送

```bash
git add -A
git commit -m "feat: 新增 XX 渠道"
git push origin main
```

### 第 2 步：触发工作流

本仓库的聚合渠道由 GitHub Actions 工作流 `crawler.yml`（名称 **Get Hot News**）执行，
每小时的**第 7 分钟**自动运行（cron: `7 * * * *`，UTC）。推送代码本身**不会**自动触发
该工作流，需要以下任一方式让它跑起来：

- **方式 A（推荐，手动触发）**：GitHub 仓库页面 → **Actions** → 左侧 **Get Hot News** →
  右侧 **Run workflow** → 选 `main` 分支 → 点击 **Run workflow**。
- **方式 B（等待定时）**：等待下一个整点第 7 分钟自动运行（最多等不到 1 小时）。

### 第 3 步：监控执行成功

- 在 **Actions → Get Hot News** 页面查看本次运行：状态应为绿色 ✅（`success`）。
- 点击该次运行 → **Run crawler** 步骤日志中，应能看到新渠道抓取记录，
  例如 `[存储推送] 已写入 latest.json，共 N 条`。
- **验收标准**：
  1. 工作流运行结果为 `success`（非失败/超时）；
  2. 日志中 `[存储推送]` 输出没有报错；
  3. 信息聚合面板刷新后能看到新渠道 Tab 且有条目（渠道 `status=ok`）。

> 💡 若某个渠道因网络/IP 限制抓取失败（如 v2ex 对数据中心 IP 受限），工作流仍会
> `success`，但该渠道 `status=error` 且面板无数据——这属于站点限制而非代码错误，
> 需确认部署环境的网络可达性。

---

## 六、相关文件索引（减少探索成本）

| 文件 | 作用 |
|------|------|
| `config/config.yaml` | **渠道配置总入口**（热榜/RSS/扩展开关） |
| `trendradar/core/loader.py` | 配置加载；`_load_aggregation_config()` 读取聚合配置 |
| `trendradar/__main__.py` | 主流程；`_publish_to_storage()` 构建并发布所有渠道 |
| `trendradar/crawler/channels.py` | 扩展渠道抓取函数（RSS/水木/InfoQ/翻译/摘要） |
| `trendradar/crawler/email_fetch.py` | 电子邮件渠道抓取 |
| `trendradar/storage/push.py` | `publish_latest()` 写入 COS `latest.json` |
| `trendradar/storage/remote.py` | COS 上传实现 |
