FROM python:3.12-slim
LABEL app=sports-platform

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt requirements-platform.txt ./
RUN pip install --no-cache-dir -r requirements-platform.txt
COPY pmbot pmbot
COPY scripts scripts
COPY core core
COPY data_engine data_engine
COPY workers workers
COPY ml_engine ml_engine
COPY ai_engine ai_engine
COPY decision_engine decision_engine
COPY backtesting backtesting
COPY migrations migrations
COPY alembic.ini .

CMD ["python", "-u", "-m", "workers.scheduler"]
