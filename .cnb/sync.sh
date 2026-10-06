#!/bin/sh
set -eu
REPO="${CNB_REPO_SLUG##*/}"
OWNER="popfbi-bot"
START=$(date +%s)
GH="https://github.com/${OWNER}/${REPO}.git"

TARGET=${TARGET:-main}

emit() {
  cat > SYNC_STATUS.md <<EOF
# 镜像同步状态

| 项 | 值 |
|---|---|
| 仓库 | \`${CNB_REPO_SLUG}\` |
| 源 | github.com/${OWNER}/${REPO} |
| 最后运行 | $(date '+%Y-%m-%d %H:%M:%S %Z') |
| 耗时 | $(( $(date +%s) - START )) 秒 |
| 结果 | ${STATUS} |
| 源默认分支 | \`${DEF:-未知}\` |

## 容器自检

| 检查项 | 结果 |
|---|---|
| git | $(git --version 2>&1 | head -1) |
| CNB_TOKEN | $([ -n "${CNB_TOKEN:-}" ] && echo "已注入(长度${#CNB_TOKEN})" || echo "未注入") |
| 访问 github.com | ${PING} |
| 当前 HEAD | \`$(git rev-parse --short HEAD 2>/dev/null || echo 无)\` |
| 当前 commit | $(git log -1 --format=%s 2>/dev/null || echo 无) |

## 详情

\`\`\`
${DETAIL}
\`\`\`
EOF
}

PING=$(timeout 30 git ls-remote "$GH" HEAD 2>&1 | head -1 | cut -c1-70) || PING="连接失败"
echo "  探测: $PING"

git remote remove upstream 2>/dev/null || true
git remote add upstream "$GH" 2>/dev/null || git remote set-url upstream "$GH"
git fetch --prune --tags upstream '+refs/heads/*:refs/remotes/upstream/*' 2>/dev/null || git fetch --prune --tags upstream

# 自动探测默认分支：优先 upstream/HEAD，否则 main/master，否则第一个分支
DEF=$(git symbolic-ref --short refs/remotes/upstream/HEAD 2>/dev/null | sed 's|^upstream/||' || true)
if [ -z "$DEF" ]; then
  if git show-ref --verify --quiet refs/remotes/upstream/main; then DEF=main
  elif git show-ref --verify --quiet refs/remotes/upstream/master; then DEF=master
  else DEF=$(git for-each-ref --format='%(refname:short)' refs/remotes/upstream/ 2>/dev/null | head -1 | sed 's|^upstream/||' || true)
  fi
fi
echo "  源默认分支: ${DEF:-未探测到}"
TARGET=$(git branch --show-current 2>/dev/null || echo main)
echo "  目标分支(CNB侧): $TARGET"
GH_HEAD=$(git rev-parse "refs/remotes/upstream/$DEF" 2>/dev/null || echo "")

if [ -z "$DEF" ] || [ -z "$GH_HEAD" ]; then
  STATUS="❌ 无法确定源分支"
  DETAIL="探测到的远端分支: $(git for-each-ref --format='%(refname:short)' refs/remotes/upstream/ 2>/dev/null | tr '\n' ' ')"
else
  IS_BOOT=$(git log -1 --format=%s 2>/dev/null | grep -ci bootstrap || echo 0)
  if [ "$IS_BOOT" != "0" ]; then
    echo "  首次同步：对齐 GitHub 的 $DEF"
    STATUS="✅ 首次同步完成"
    cp -f .cnb.yml /tmp/keep.cnb.yml 2>/dev/null || true
    cp -rf .cnb /tmp/keepcnb 2>/dev/null || true
    git reset --hard "refs/remotes/upstream/$DEF"
    [ -f .cnb.yml ] || { [ -f /tmp/keep.cnb.yml ] && cp -f /tmp/keep.cnb.yml .cnb.yml; } || true
    mkdir -p .cnb
    [ -f .cnb/sync.sh ] || { [ -f /tmp/keepcnb/sync.sh ] && cp -f /tmp/keepcnb/sync.sh .cnb/sync.sh; } || true
    git add -A
    git -c user.email=c@x -c user.name=sync commit -q -m "chore: 首次同步自 GitHub $DEF（保留同步配置）" || true
    git push --force origin "HEAD:refs/heads/$TARGET" || true
    DETAIL="bootstrap → $DEF $(git rev-parse --short HEAD)，$(git rev-list --count HEAD) commits"
  else
    CNB_HEAD=$(git rev-parse HEAD)
    if [ "$GH_HEAD" = "$CNB_HEAD" ]; then
      STATUS="✅ 已是最新"; DETAIL="两侧 HEAD 一致 $(git rev-parse --short HEAD)"
    else
      echo "  增量快进 $DEF"
      git merge --ff-only "refs/remotes/upstream/$DEF"
      git push origin "HEAD:refs/heads/$TARGET"
      NEW=$(git rev-parse HEAD)
      if [ "$NEW" = "$GH_HEAD" ]; then STATUS="✅ 同步完成"; DETAIL="${CNB_HEAD:0:8} → ${NEW:0:8}"; else STATUS="⚠️ 部分同步"; DETAIL="期望 ${GH_HEAD:0:8} 实际 ${NEW:0:8}"; fi
    fi
  fi
fi

emit
git add SYNC_STATUS.md 2>/dev/null || true
git -c user.email=c@x -c user.name=sync commit -q -m "sync: 更新状态" || true
git push origin "HEAD:refs/heads/$TARGET" || true
echo "SYNC_DONE"
