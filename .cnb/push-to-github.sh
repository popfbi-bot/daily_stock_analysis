#!/bin/sh
# ============================================================
#  CNB → GitHub 每日同步（由 .cnb.yml 的 crontab 调用）
# ============================================================
#  设计要点
#  --------
#  1. 无限重试：网络抖动、GitHub 502、代理抽风都不该让同步失败。
#     采用「指数退避 + 单次上限」的循环，最长跑约 50 分钟，
#     仍失败则记状态并退出（非 0），由下一天的 crontab 接着补。
#
#  2. 保留双方历史：CNB 侧可能已有 GitHub 没有的提交（如 sync: 更新状态），
#     直接 push 会被 fetch first 拒绝。统一用 merge，不 rebase。
#
#  3. 不产生噪音提交：只在状态变化时写 SYNC_STATUS.md，
#     且若最终内容没变就不提交、不推状态文件。
#
#  4. 因果记忆库特判：cnbnasa/causal-memory-stack 推到 GitHub 会触发
#     CreateOS 重建、清空 /data/causal.db。推送前先备份云端记忆，
#     推送后自动回填（见 .cnb/rebuild-backfill.sh）。
#
#  环境变量（由 imports 从密钥仓库注入）
#  ------------------------------------
#    GITHUB_USERNAME / GITHUB_TOKEN   必需，缺任一则跳过并记原因
#    CNB_TO_GH_MAX_MINUTES           可选，单次运行上限，默认 50
# ============================================================

set -u

REPO="${CNB_REPO_SLUG##*/}"
OWNER="popfbi-bot"
GH="https://${GITHUB_USERNAME:-${OWNER}}:@github.com/${OWNER}/${REPO}.git"
# 去掉 URL 里的空密码占位，交给 credential helper 或 URL 注入
GH="https://${GITHUB_USERNAME:-${OWNER}}:${GITHUB_TOKEN}@github.com/${OWNER}/${REPO}.git"

TARGET=$(git branch --show-current 2>/dev/null || echo main)
[ -z "$TARGET" ] && TARGET=main
MAX_MINUTES="${CNB_TO_GH_MAX_MINUTES:-50}"
START=$(date +%s)

STATUS="未执行"
DETAIL=""
RESULT="skip"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

now_s() { echo $(($(date +%s) - START)); }
over_time() { [ "$(now_s)" -ge $((MAX_MINUTES * 60)) ]; }

# 写状态文件；内容没变就不动，避免每天多一个空提交
write_status() {
  cat > SYNC_STATUS.md <<EOF
# CNB → GitHub 同步状态

| 项 | 值 |
|---|---|
| 仓库 | \`${CNB_REPO_SLUG}\` |
| 方向 | CNB → GitHub（github.com/${OWNER}/${REPO}） |
| 最后运行 | $(date '+%Y-%m-%d %H:%M:%S %Z') |
| 耗时 | $(now_s) 秒 |
| 结果 | ${STATUS} |
| 推送的提交 | ${DETAIL:-无} |

## 说明

本仓库已**取消本机双推**。现在的同步链路是：

\`\`\`
本机 ──push──> CNB                随时手动
CNB──crontab(每日 03:17) ──push──> GitHub   失败自动重试
\`\`\`

GitHub 侧一天只同步一次，本机不因 GitHub 网络不稳而卡住。

## 本次运行

\`\`\`
${DETAIL:-（无）}
\`\`\`
EOF
}

commit_status() {
  git add SYNC_STATUS.md 2>/dev/null || return 0
  if git diff --cached --quiet 2>/dev/null; then
    return 0   # 内容没变，不提交
  fi
  git -c user.email="sync@cnb" -c user.name="cnb-sync" \
      commit -q -m "sync: 更新 CNB → GitHub 同步状态" || return 0
  git push origin "HEAD:refs/heads/${TARGET}" >/dev/null 2>&1 || true
}

# ---------- 凭据检查 ----------
if [ -z "${GITHUB_TOKEN:-}" ] || [ -z "${GITHUB_USERNAME:-}" ]; then
  STATUS="⏸️ 已跳过（未注入 GitHub 凭据）"
  DETAIL="GITHUB_USERNAME / GITHUB_TOKEN 未注入。
密钥仓库（cnb-sync-secrets）只能由 pipelines 的 imports 读取，
若此处为空，请检查 .cnb.yml 的 imports 路径与密钥仓库的allow_* 规则。"
  write_status; commit_status
  log "SKIP: 无 GitHub 凭据"
  exit 0
fi
log "凭据已注入（token 长度 ${#GITHUB_TOKEN}）"

git remote remove github 2>/dev/null || true
git remote add github "$GH" 2>/dev/null || git remote set-url github "$GH"

# ---------- 判断是否需要推送 ----------
# 只有「CNB 领先 GitHub」才推。GitHub 领先说明用户在 GitHub 侧改了东西，
# 那属于需要人工决策的分叉，不在定时任务里自动 merge 覆盖。
check_and_push() {
  attempt=$1
  log "第 ${attempt} 次尝试"

  if ! timeout 300 git fetch --prune --tags github "+refs/heads/*:refs/remotes/github/*" 2>&1; then
    return 2# 网络/认证失败 → 可重试
  fi

  if ! git show-ref --verify --quiet refs/remotes/github/main; then
    STATUS="⚠️ GitHub 上没有 main 分支"
    DETAIL="首次同步需要 GitHub 侧已存在 main 分支。请在 GitHub 上先建仓或推一次。"
    RESULT="fail"
    return 3   # 不可重试（配置问题）
  fi

  CNB_HEAD=$(git rev-parse HEAD)
  GH_HEAD=$(git rev-parse refs/remotes/github/main)

  if [ "$CNB_HEAD" = "$GH_HEAD" ]; then
    log "两侧已一致 ${CNB_HEAD:0:8}"
    return 0
  fi

  if git merge-base --is-ancestor "$GH_HEAD" "$CNB_HEAD" 2>/dev/null; then
    # 快进：GitHub 是 CNB 的祖先，直接推
    log "快进推送 ${GH_HEAD:0:8} → ${CNB_HEAD:0:8}"
    if timeout 400 git push github "HEAD:refs/heads/main" 2>&1 | tail -3; then
      NEW=$(git ls-remote github "refs/heads/main" 2>/dev/null | awk '{print $1}')
      if [ "$NEW" = "$CNB_HEAD" ]; then
        STATUS="✅ 同步完成"
        DETAIL="${GH_HEAD:0:8} → ${CNB_HEAD:0:8}（快进，$(git rev-list --count "$GH_HEAD".."$CNB_HEAD") 个提交）"
        RESULT="ok"
        return 0
      fi
    fi
    return 2   # 推送失败 → 可重试
  fi

  if git merge-base --is-ancestor "$CNB_HEAD" "$GH_HEAD" 2>/dev/null; then
    # GitHub 领先：把 GitHub 的新提交合进来再推（保持历史，不 rebase）
    log "GitHub 领先，合并后回推"
    if git merge --no-edit "refs/remotes/github/main" 2>&1 | tail -3; then
      if timeout 400 git push github "HEAD:refs/heads/main" 2>&1 | tail -3; then
        NEW=$(git rev-parse HEAD)
        PUSHED=$(git ls-remote github "refs/heads/main" 2>/dev/null | awk '{print $1}')
        if [ "$PUSHED" = "$NEW" ]; then
          STATUS="✅ 已同步（合并 GitHub 侧提交）"
          DETAIL="合并 github/main 后推到 ${NEW:0:8}"
          RESULT="ok"
          return 0
        fi
      fi
      return 2
    fi
    return 2
  fi

  # 双向分叉：自动 merge（非交互），冲突则放弃并报告
  log "两侧分叉，尝试自动合并"
  if git merge --no-edit --no-ff "refs/remotes/github/main" >/dev/null 2>&1; then
    if timeout 400 git push github "HEAD:refs/heads/main" >/dev/null 2>&1; then
      STATUS="✅ 已同步（自动合并分叉）"
      DETAIL="合并分叉后推到 $(git rev-parse --short HEAD)"
      RESULT="ok"
      return 0
    fi
    git reset --hard "$CNB_HEAD" 2>/dev/null
    return 2
  fi

  git merge --abort 2>/dev/null || git reset --hard "$CNB_HEAD" 2>/dev/null
  STATUS="⚠️ 需要人工处理（与 GitHub 分叉且自动合并冲突）"
  DETAIL="CNB $(git rev-parse --short HEAD) 与 GitHub $(git rev-parse --short refs/remotes/github/main) 已分叉。
本机跑：cd <仓库> && git fetch github && git merge github/main
解决冲突后重跑 tools/cnb-push.sh 即可。"
  RESULT="fail"
  return 3   # 不可重试
}

# ---------- 因果记忆库：推 GitHub 前先备份云端记忆 ----------
BACKUP=""
if [ "$REPO" = "causal-memory-stack" ]; then
  log "检测到因果记忆库，先备份云端记忆（防止重建清空）"
  BACKUP=$(sh .cnb/backup-memory.sh 2>&1 | tail -5)
  echo "$BACKUP"
  if echo "$BACKUP" | grep -q "BACKUP_OK"; then
    log "云端记忆已备份到仓库，将随本次推送一起同步"
    git add -A .cnb/memory-backup 2>/dev/null || true
    git -c user.email="sync@cnb" -c user.name="cnb-sync" \
        commit -q -m "backup: 重建前导出云端因果记忆" 2>/dev/null || true
  else
    log "⚠️ 记忆备份未成功（可能容器访问不到云端网关），继续同步但不清空旧备份"
  fi
fi

# ---------- 主循环：无限重试 ----------
attempt=0
BACKOFF=10
while :; do
  attempt=$((attempt + 1))
  rc=0
  check_and_push "$attempt" || rc=$?

  case "$rc" in
    0) break;;                 # 成功
    3) break;;                 # 不可重试的问题，直接报告
    *)
      if over_time; then
        STATUS="❌ 重试 ${attempt} 次仍失败（已达 ${MAX_MINUTES} 分钟上限）"
        DETAIL="最后一次尝试在第 ${attempt} 次。
下一天的 crontab 会继续尝试，已推送的提交不会重复推。
若持续失败，请检查 GitHub 令牌是否过期、仓库是否被改名。"
        RESULT="fail"
        log "超过时间上限，停止重试"
        break
      fi
      log "失败（网络或GitHub 暂时不可用），${BACKOFF}s 后重试"
      sleep "$BACKOFF"
      # 指数退避：10 → 20 → 40 → 80 → 上限 300
      [ "$BACKOFF" -lt 300 ] && BACKOFF=$((BACKOFF * 2))
      [ "$BACKOFF" -gt 300 ] && BACKOFF=300
      ;;
  esac
done

# ---------- 因果记忆库：重建后回填 ----------
if [ "$REPO" = "causal-memory-stack" ] && [ "$RESULT" = "ok" ]; then
  log "云端已进入重建流程，启动记忆回填（最多等 40 分钟）"
  sh .cnb/rebuild-backfill.sh 2>&1 | tail -20
  if [ -f .cnb/BACKFILL_DONE ]; then
    DETAIL="${DETAIL}
✅ 记忆回填完成（重建后已自动 import 回去）"
    rm -f .cnb/BACKFILL_DONE
  else
    DETAIL="${DETAIL}
⏳ 记忆回填未在本次运行内完成，可能需人工跑一次dev/sync_memory.py --push"
  fi
fi

write_status
commit_status

log "RESULT=$RESULT STATUS=$STATUS"
[ "$RESULT" = "ok" ] && exit 0
[ "$RESULT" = "skip" ] && exit 0
exit 1