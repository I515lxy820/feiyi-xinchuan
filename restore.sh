#!/bin/bash
# ========== AI素养助手一键恢复脚本 ==========
# 沙盒重置后运行此脚本即可恢复全部数据和服务
# ============================================
set -e

BACKUP_DIR="/data/user/backup_ai_assistant"
WORKSPACE="/workspace"
ASSISTANT="$WORKSPACE/ai-literacy-assistant"
BANK="$WORKSPACE/question_bank"

echo "======================================"
echo " AI素养助手 - 数据恢复"
echo "======================================"

# 检查备份是否存在
if [ ! -d "$BACKUP_DIR" ]; then
  echo "❌ 跨挂载点备份不存在: $BACKUP_DIR"
  exit 1
fi

echo "📦 备份来源: $BACKUP_DIR"
echo ""

# 1. 恢复题库
echo "[1/4] 恢复题库..."
mkdir -p "$BANK"
cp -rf "$BACKUP_DIR/question_bank/"* "$BANK/" 2>/dev/null || true
echo "  ✅ 题库已恢复 ($(ls $BANK/*.json 2>/dev/null | wc -l) 个文件)"

# 2. 恢复助手服务
echo "[2/4] 恢复助手服务代码..."
mkdir -p "$ASSISTANT"
cp -rf "$BACKUP_DIR/app" "$ASSISTANT/" 2>/dev/null || true
cp -rf "$BACKUP_DIR/static" "$ASSISTANT/" 2>/dev/null || true
cp -rf "$BACKUP_DIR/scripts" "$ASSISTANT/" 2>/dev/null || true
cp -rf "$BACKUP_DIR/tests" "$ASSISTANT/" 2>/dev/null || true
cp -rf "$BACKUP_DIR/data" "$ASSISTANT/" 2>/dev/null || true
cp -f "$BACKUP_DIR/.env" "$ASSISTANT/.env" 2>/dev/null || true
echo "  ✅ 服务代码已恢复"

# 3. 安装依赖 + 导入题库
echo "[3/4] 安装依赖并初始化数据库..."
cd "$ASSISTANT"
pip install -r requirements.txt -q 2>/dev/null || true
mkdir -p data
if [ -f "$BANK/question_bank.json" ]; then
  python3 scripts/import_questions.py "$BANK/question_bank.json" --split practice
fi

# 4. 启动服务
echo "[4/4] 启动助手服务 (端口 8010)..."
pkill -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1
nohup python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8010 > /workspace/service.log 2>&1 &
sleep 2

# 验证
HEALTH=$(curl -s http://localhost:8010/api/health 2>/dev/null || echo "FAIL")
echo ""
echo "======================================"
echo " 恢复完成！"
echo "======================================"
echo " 服务状态: $HEALTH"
echo " 题库文件: $BANK/question_bank.json"
echo " SQLite:   $ASSISTANT/data/app.db"
echo ""
echo " 如果 health 返回 ok ，说明一切正常。"
echo "======================================"
