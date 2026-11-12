#!/bin/bash
# ========== AI素养助手自动备份脚本 ==========
# 每次采集完运行此脚本，多重备份确保不丢数据
# ============================================
set -e

BACKUP_DIR="/data/user/backup_ai_assistant"
WORKSPACE="/workspace"

echo "💾 开始备份..."

# 1. 跨挂载点 rsync 备份 (最可靠)
rsync -av --delete \
  "$WORKSPACE/question_bank/" \
  "$WORKSPACE/ai-literacy-assistant/.env" \
  "$WORKSPACE/ai-literacy-assistant/data/" \
  "$WORKSPACE/ai-literacy-assistant/app/" \
  "$WORKSPACE/ai-literacy-assistant/static/" \
  "$WORKSPACE/ai-literacy-assistant/scripts/" \
  "$WORKSPACE/ai-literacy-assistant/tests/" \
  "$BACKUP_DIR/" 2>&1 | tail -3

# 2. SQLite 数据库单独拷一份
cp -f "$WORKSPACE/ai-literacy-assistant/data/app.db" \
      "$WORKSPACE/question_bank/app.db.backup" 2>/dev/null || true

# 3. 全量打包一个时间戳快照
TS=$(date +%Y%m%d_%H%M%S)
tar czf "/data/user/backup_snapshot_${TS}.tar.gz" \
  -C "$WORKSPACE" ai-literacy-assistant question_bank 2>/dev/null || true
# 只保留最近5个快照
ls -t /data/user/backup_snapshot_*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f

# 4. git 提交
cd "$WORKSPACE" && git add -A && \
  git commit -m "backup: $(date '+%Y-%m-%d %H:%M')" 2>/dev/null || true

# 统计
BANK_COUNT=$(python3 -c "import json; print(len(json.load(open('$WORKSPACE/question_bank/question_bank.json'))))" 2>/dev/null || echo "?")
echo "✅ 备份完成 | 题库 ${BANK_COUNT} 题 | $(date '+%H:%M:%S')"
