#!/usr/bin/env bash
# 部署板端「识图问答」服务到 ATK-DLRK3568。
#
#   BOARD=10.176.240.215 bash deploy_to_board.sh
#
# 只做四件事：scp -> 语法检查 -> 安装 unit -> 重启并验证。
# 不会碰视觉/雷达/融合/收银等已有服务。
#
# 板端 IP 跟着热点变，下面的默认值只是「写这个脚本时的地址」—— 一律显式传 BOARD=。

set -euo pipefail

BOARD="${BOARD:-10.176.240.215}"
PC="${PC:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
[ -z "$PC" ] && PC="${PC_IP:-}"
KEY="${KEY:-$HOME/.ssh/id_ed25519_rk3568}"
PORT="${PORT:-8092}"
HERE="$(cd "$(dirname "$0")" && pwd)"

SSH_OPTS=(-i "$KEY" -o IPQoS=none -o ConnectTimeout=30
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR)
ssh_run() { ssh "${SSH_OPTS[@]}" "linaro@${BOARD}" "$@"; }
scp_to()  { scp "${SSH_OPTS[@]}" "$1" "linaro@${BOARD}:$2"; }

if [ -z "$PC" ]; then
  echo "用法: BOARD=<板端IP> PC=<电脑IP> bash deploy_to_board.sh" >&2
  exit 1
fi

echo "==> 板端 ${BOARD} / 电脑侧 VLM http://${PC}:8097"

echo "==> 1/5 建立目录"
ssh_run 'mkdir -p /home/linaro/ai/vlm'

echo "==> 2/5 上传文件"
scp_to "${HERE}/board/vlm_client_service.py" /home/linaro/ai/vlm/
scp_to "${HERE}/board/rk3568-vlm.service"     /tmp/rk3568-vlm.service
scp_to "${HERE}/README.md"                    /home/linaro/ai/vlm/README.md 2>/dev/null || true

echo "==> 3/5 语法检查 + 写配置"
ssh_run "python3 -m py_compile /home/linaro/ai/vlm/vlm_client_service.py && echo '  语法 OK'"
ssh_run "printf '%s' '{\"pc_url\": \"http://${PC}:8097\", \"vision_url\": \"http://127.0.0.1:8088\", \"port\": ${PORT}}' > /home/linaro/ai/vlm/vlm_client_config.json && echo '  配置 OK'"
ssh_run "chmod 755 /home/linaro/ai/vlm/vlm_client_service.py"

echo "==> 4/5 安装 systemd unit"
ssh_run "sudo mv /tmp/rk3568-vlm.service /etc/systemd/system/rk3568-vlm.service && sudo systemctl daemon-reload && sudo systemctl enable rk3568-vlm.service && sudo systemctl restart rk3568-vlm.service"

echo "==> 5/5 验证"
sleep 3
ssh_run "systemctl is-active rk3568-vlm.service"
ssh_run "curl -s --noproxy '*' -m 20 http://127.0.0.1:${PORT}/api/vlm/status"
echo
echo "完成。网页: http://${BOARD}:${PORT}/"
echo "板端自检: ssh linaro@${BOARD} 'python3 /home/linaro/ai/vlm/vlm_client_service.py --check'"
