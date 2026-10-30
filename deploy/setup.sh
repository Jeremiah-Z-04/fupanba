#!/usr/bin/env bash
# 复盘吧 · 云服务器一键部署
#
# 用法（在服务器上、用 root）：
#   tar -xzf fupanba_deploy_YYYYMMDD.tar.gz -C /opt
#   sudo bash /opt/fupanba/deploy/setup.sh
#
# 做的事：装依赖 → 建虚拟环境 → 生成访问密码 → 装 systemd 开机自启
#         → 配工作日 15:40 自动采集 → 配每日 03:20 备份
# 可重复运行：密码和配置只在第一次生成，之后原样保留。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/fupanba}"
PORT="${PORT:-8765}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m注意:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash deploy/setup.sh"

# ---------- 1. 系统依赖 ----------
log "安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl cron >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 python3-pip curl cronie >/dev/null
  systemctl enable --now crond >/dev/null 2>&1 || true
else
  die "只支持 apt / dnf 系发行版（Ubuntu、Debian、Alibaba Cloud Linux、CentOS）"
fi

# ---------- 2. 时区 ----------
# cron 按系统时区决定何时触发，服务器默认往往是 UTC，不改会差 8 小时
if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-timezone Asia/Shanghai 2>/dev/null || warn "设置时区失败，请手动执行 timedatectl set-timezone Asia/Shanghai"
else
  ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime 2>/dev/null || true
fi

# ---------- 3. 落地文件 ----------
if [ "$SRC" != "$APP_DIR" ]; then
  log "复制文件到 $APP_DIR"
  mkdir -p "$APP_DIR"
  cp -a "$SRC/." "$APP_DIR/"
fi
mkdir -p "$APP_DIR/data/logs" "$APP_DIR/data/backups"
chmod +x "$APP_DIR/deploy/"*.sh 2>/dev/null || true

# ---------- 4. Python 环境 ----------
log "创建虚拟环境并安装依赖（首次约 1 分钟）"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements-server.txt"

# ---------- 5. 访问密码 ----------
ENV_FILE="$APP_DIR/.service.env"
NEW_PASS=""
if [ ! -f "$ENV_FILE" ]; then
  NEW_PASS=$(python3 -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(12)))")
  cat > "$ENV_FILE" <<EOF
TZ=Asia/Shanghai
TH_AUTH=admin:$NEW_PASS
EOF
  chmod 600 "$ENV_FILE"
  log "已生成访问密码"
else
  log "沿用已有密码配置"
fi

# ---------- 6. 同花顺 API Key ----------
if [ ! -f "$APP_DIR/.env" ]; then
  echo
  echo "需要同花顺 API Key（fuyao.aicubes.cn）。"
  echo "如果部署包里已经带了 .env 会自动跳过这一步。"
  read -r -p "粘贴 Key（直接回车跳过，之后可手工写入 $APP_DIR/.env）: " KEY || KEY=""
  if [ -n "${KEY:-}" ]; then
    printf 'HITHINK_FINANCE_API_KEY=%s\n' "$KEY" > "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
  else
    warn "没有 Key，采集会失败；之后补写 $APP_DIR/.env 再 systemctl restart fupanba"
  fi
fi

# ---------- 7. systemd 服务 ----------
log "安装开机自启服务"
sed "s|/opt/fupanba|$APP_DIR|g" "$APP_DIR/deploy/fupanba.service" > /etc/systemd/system/fupanba.service
systemctl daemon-reload
systemctl enable fupanba >/dev/null 2>&1
systemctl restart fupanba

# ---------- 8. 定时任务 ----------
log "配置定时任务：工作日 15:40 自动复盘，每日 03:20 备份"
TMP_CRON=$(mktemp)
crontab -l 2>/dev/null | grep -v "fupanba" > "$TMP_CRON" || true
cat >> "$TMP_CRON" <<EOF
40 15 * * 1-5 cd $APP_DIR && TZ=Asia/Shanghai .venv/bin/python main.py run --no-clip >> data/logs/cron.log 2>&1
20 3 * * * APP_DIR=$APP_DIR bash $APP_DIR/deploy/backup.sh >> $APP_DIR/data/logs/backup.log 2>&1
EOF
crontab "$TMP_CRON"
rm -f "$TMP_CRON"

# ---------- 9. 结果 ----------
sleep 3
echo
if systemctl is-active --quiet fupanba; then
  PUB_IP=$(curl -s --max-time 4 https://api.ipify.org 2>/dev/null || true)
  [ -n "$PUB_IP" ] || PUB_IP=$(curl -s --max-time 4 https://ifconfig.me 2>/dev/null || true)
  [ -n "$PUB_IP" ] || PUB_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  PASS_SHOW="$NEW_PASS"
  [ -n "$PASS_SHOW" ] || PASS_SHOW="（沿用 .service.env 里的旧密码）"
  cat <<EOF

  ┌──────────────────────────────────────────────┐
    复盘吧已启动

    访问地址   http://$PUB_IP:$PORT
    用户名     admin
    密码       $PASS_SHOW

    常用命令
      systemctl status fupanba      查看状态
      systemctl restart fupanba     重启
      journalctl -u fupanba -n 50   看日志
      tail -f $APP_DIR/data/logs/cron.log   看自动采集

  └──────────────────────────────────────────────┘
EOF
  warn "还没放行端口：去云控制台「安全组 / 防火墙」开放 TCP $PORT，否则打不开"
  warn "当前是 HTTP 明文 + Basic 认证，密码会在公网传输；要更安全就绑域名配 HTTPS（见 README）"
else
  die "服务没起来，执行 journalctl -u fupanba -n 50 看原因"
fi
