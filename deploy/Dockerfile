# One small image that runs the cache (the default command), the usage agent or the join
# service, and holds the certificate and configuration tools:
#
#   docker build -f deploy/Dockerfile -t lean-pool .
#   docker run lean-pool leanpool-cache --help
#   docker run -p 18200:18200 lean-pool leanpool-agent
#   docker run -p 18110:18110 lean-pool leanpool-join --help
#   docker run lean-pool leanpool-pki --help
#
# Build from the repository root (the build context must contain pyproject.toml and leanpool/).

FROM python:3.12-slim AS build
WORKDIR /source
COPY pyproject.toml README.md LICENSE ./
COPY leanpool ./leanpool
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
COPY --from=build /install /usr/local

# A fixed, unprivileged user. The cache writes only below /var/lib/leanpool; mount a volume there.
RUN groupadd --gid 10001 leanpool \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin leanpool \
    && install -d -o leanpool -g leanpool /var/lib/leanpool
USER 10001:10001
WORKDIR /var/lib/leanpool
VOLUME ["/var/lib/leanpool"]

ENV LEANPOOL_CACHE_DATABASE=/var/lib/leanpool/cache.sqlite3 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 18102: the cache. 18200: the usage agent. 18110: the join service, while a window is open.
EXPOSE 18102 18110 18200
CMD ["leanpool-cache"]
