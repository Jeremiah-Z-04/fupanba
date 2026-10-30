# 部署：两条路，按需求选

| | A. GitHub Pages | B. 云服务器 |
|---|---|---|
| 费用 | 0 | 100–300 元/年 |
| 电脑关机也能看 | ✅ | ✅ |
| 手机添加到主屏幕 | ✅ 真全屏（有 HTTPS） | 要配证书才全屏 |
| 改龙头排序 / 补全候选 / 题材框架 | ❌ 只能看 | ✅ 完整功能 |
| 生成导入图片 / TXT / 复制代码 | ✅ | ✅ |
| 自动采集 | ❌ 要本地跑一次再推 | ✅ cron 每天 15:40 |
| 部署难度 | 低（git push） | 中（买机 + 装系统） |
| 国内访问 | 一般，偶尔慢 | 快 |

**建议**：先做 A，10 分钟能上线。等你确认手机上真的用得上，再决定要不要为「能改排序」花 300 块上 B。

两条路可以并存——同一份代码既能跑服务端，也能导出静态站。

---

# A. GitHub Pages（免费只读版）

## 1. 建仓库

在 GitHub 上新建一个仓库：

- 名字随意，比如 `fupanba`
- **必须 Public**（免费账号的 Pages 只支持公开仓库；私有仓库部署页面要 GitHub Pro）
- 不要勾选「Add a README」

> 公开仓库意味着你的复盘数据对所有人可见。只是看题材和龙头、不涉及仓位，一般没问题；
> 介意的话就走 B 方案（服务器 + 密码）。

## 2. 本地接上仓库

```bash
cd E:/复盘吧
git init -b main
git remote add origin https://github.com/你的用户名/fupanba.git
git add .
git commit -m "首次提交"
git push -u origin main
```

`.gitignore` 已经排除了 `.env`（API Key）和 `data/`（本地数据库），不会误传。

## 3. 导出并发布

```bash
python deploy/publish_static.py
```

它做三件事：导出全部历史数据到 `docs/data/` → 提交 → 推送。

想只保留最近 120 个交易日：

```bash
python deploy/publish_static.py --days 120
```

## 4. 开启 Pages

仓库 → **Settings → Pages**：

- Source 选 `Deploy from a branch`
- Branch 选 `main`，目录选 **`/docs`**
- Save

约 1 分钟后访问 `https://你的用户名.github.io/fupanba/`。

## 5. 手机加到主屏幕

打开上面的地址 → 输入框都不需要（只读站没有密码）：

- iOS Safari：分享 → 添加到主屏幕
- Android Chrome：⋮ → 添加到主屏幕

因为有 HTTPS，点开后是全屏的，跟 App 一样。

## 6. 每天更新

收盘后双击 **`daily_publish.bat`**，它会跑复盘 → 导出 → 推送。

也可以分步手动：

```bash
python main.py run --no-clip        # 采集 + 复盘
python deploy/publish_static.py     # 导出 + 推送
```

数据没变化时不会产生空提交。

## 7. 国内访问慢怎么办

`github.io` 在国内时快时慢，移动网络下偶尔打不开。两个办法：

**办法一：Cloudflare Pages（同一个仓库，更快）**

1. 注册 Cloudflare → Workers & Pages → Create → Pages → 连接 Git
2. 选这个仓库，构建命令留空，输出目录填 `docs`
3. 得到一个 `xxx.pages.dev` 域名

国内一般比 github.io 稳，且自带 HTTPS。

**办法二：绑自定义域名**

买域名（几十块）→ Cloudflare 加 DNS → 绑定到 Pages。
不挂国内服务器就不需要备案。

---

# B. 云服务器（完整功能）

需要手机也能改排序、补全候选、跑采集时选这条。

## 0. 先验证 API 会不会拒绝陌生 IP

手机开热点 → 电脑**断开原 WiFi** 只连热点 → 打开复盘吧点「采集数据」。

- 能拉到涨停池 → 继续
- 报 401 / 403 / 4001 → Key 绑了 IP，云上跑不通

## 1. 买服务器

| 项目 | 建议 |
|---|---|
| 厂商 | 腾讯云轻量 / 阿里云轻量 |
| 配置 | 2 核 2G、40G、3M，够用 |
| 系统 | **Ubuntu 22.04 / 24.04** |
| 价格 | 新人首年百元级 |

## 2. 打包上传

```bash
python deploy/package.py
scp release\fupanba_deploy_*.tar.gz root@服务器IP:/opt/
```

## 3. 一键部署

```bash
cd /opt && tar -xzf fupanba_deploy_*.tar.gz
sudo bash /opt/fupanba/deploy/setup.sh
```

自动完成：装依赖、建 venv、生成随机密码、systemd 开机自启、时区、cron（工作日 15:40 复盘 + 每日 03:20 备份）。

跑完打印访问地址和密码。

## 4. 放行端口

这是最容易漏的一步：

- 腾讯云：轻量 → 防火墙 → 添加 TCP 8765
- 阿里云：ECS → 安全组 → 入方向 8765

## 5. 日常命令

```bash
systemctl status fupanba
systemctl restart fupanba
journalctl -u fupanba -n 50
tail -f /opt/fupanba/data/logs/cron.log
```

## 6. 升级

```bash
python deploy/package.py --no-db     # 本地，不带数据库
scp release\fupanba_deploy_*.tar.gz root@IP:/opt/
cd /opt && tar -xzf fupanba_deploy_*.tar.gz
sudo systemctl restart fupanba
```

`--no-db` 保证不覆盖服务器上的历史数据。

## 7. 域名 + HTTPS（可选）

想要 PWA 全屏或不想密码明文传输：

```
fupanba.你的域名.com {
    reverse_proxy 127.0.0.1:8765
}
```

装 Caddy 放这个 Caddyfile，再把 `fupanba.service` 里的 `--host 0.0.0.0` 改成 `127.0.0.1`。

国内服务器用 80/443 绑域名需要备案；不想备案就继续用「IP:8765」。

---

# C. 题材框架怎么让朋友自动更新

这是独立于「应用更新」的一条通道，原因很实在：

> 应用更新包会**刻意保留** `data/` 和 `config/`（不覆盖用户的数据库和配置），
> 而题材框架就存在 `data/themehunter.db` 的 `kpl_theme` 表里。
> 所以就算发了新 exe，朋友的框架也不会跟着变。

## 你的操作：改了框架就发一次

```bash
python deploy/publish_framework.py
```

它会把 `kpl_theme` 整表导出成 `docs/framework.json` 推到 GitHub。
框架内容没变时版本号不变，不会产生空提交（每天跑一次也无所谓）。

`daily_publish.bat` 已经包含这一步。

## 朋友端的行为

- **打包版 exe 启动 6 秒后**自动拉一次（源码运行不触发，免得把你本地还没发布的改动冲掉）
- 也可以在「题材框架」面板里点「同步框架」手动拉

拉取地址：

```
https://raw.githubusercontent.com/Jeremiah-Z-04/fupanba/main/docs/framework.json
https://cdn.jsdelivr.net/gh/Jeremiah-Z-04/fupanba@main/docs/framework.json
```

主地址失败自动换备用。两个都不通就跳过，不影响正常使用。

## 规则和代价

- **你是唯一真源**，朋友端整表覆盖
- 版本用内容哈希比对，内容没变就不动数据库
- 远程返回不足 100 条会拒绝覆盖（防止拉到错误页面把框架写空）
- **朋友自己手工加的题材会被覆盖掉** —— 想保留就别开自动同步（把 `update.framework_auto_sync` 设为 false）

## 第一次发版要注意

朋友手里如果是旧 exe，里面**没有**同步代码，必须手动发一次新 exe；
之后框架变化就全自动了。

---

# 排错

**Pages 打开是 404**
Pages 还没构建完（等 1–2 分钟），或者 Branch/目录选错了（要选 `/docs`）。

**页面能开但没数据**
`docs/data/index.json` 没推上去。检查 `.gitignore` 里是不是写成了 `data/`（会连 docs/data 一起忽略，必须是 `/data/`）。

**手机上图表空白**
`docs/data/YYYY-MM-DD.json` 路径不对，或者日期不在 `index.json` 里。看浏览器控制台。

**服务器打不开**
先在服务器上 `curl -I http://127.0.0.1:8765`。通了就是安全组没放行。

**密码忘了**
`cat /opt/fupanba/.service.env`
