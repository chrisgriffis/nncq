# The hosted read-only demo: nncq serving the bundled orbitkit sample (demo/). Indexed and seeded at build time,
# so a cold start only boots the server.
#   docker build -t nncq-demo . && docker run --rm -p 8080:8080 nncq-demo     -> http://localhost:8080/
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e .
COPY demo ./demo
RUN python demo/run.py --prepare --no-browser
ENV NNCQ_HOST=0.0.0.0 NNCQ_PORT=8080 NNCQ_PUBLIC=1 NNCQ_WATCH=false
EXPOSE 8080
CMD ["python", "demo/run.py", "--no-browser"]
