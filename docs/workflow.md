# 1. 啟動 DB
docker compose up -d

# 2. 爬資料（一次性）
python scripts/crawler.py

# 3. 建立索引
python data_update.py --rebuild

# 4. 查詢
python rag_query.py --query "What are 2025's hottest AI verticals?"

# 5. 產出 skill.md（需要 LITELLM_API_KEY）
python skill_builder.py --output skill.md

停止 Docker 容器

cd C:\Users\lexho\Documents\AI_systems\AIASE2026_HW3
docker compose down