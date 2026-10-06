FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/data/timeline.db \
    ATTACH_DIR=/data/attachments

WORKDIR /app
COPY app/ /app/

# IANA 时区库：跨地区事件依赖系统 zoneinfo 数据（Asia/Shanghai、
# America/New_York 等）。slim 镜像默认不含 /usr/share/zoneinfo，用系统包
# 补齐，不引入任何 pip 依赖。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/reviews')"

# 启动时自动初始化数据库（建表），随后提供服务
CMD ["python", "server.py"]
