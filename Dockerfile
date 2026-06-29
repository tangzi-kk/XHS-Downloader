# ---- 阶段 1: 构建器 ----
FROM python:3.12-bullseye as builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix="/install" -r requirements.txt


# ---- 阶段 2: 最终运行镜像 ----
FROM python:3.12-slim

WORKDIR /app

LABEL name="XHS-Downloader" \
      authors="JoeanAmier" \
      repository="https://github.com/JoeanAmier/XHS-Downloader"

# 安装 ffmpeg：
# 用于从成功下载的 MP4 中截取第一帧，生成飞书「视频封面」
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

COPY source /app/source
COPY main.py /app/main.py

EXPOSE 5556

VOLUME /app/Volume

CMD ["python", "main.py", "api"]
