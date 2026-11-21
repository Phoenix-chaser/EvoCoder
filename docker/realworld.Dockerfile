FROM python:3.12-slim
RUN python -m pip install --no-cache-dir pytest==8.3.5 click==8.1.8 packaging==24.2 pluggy==1.5.0 iniconfig==2.0.0 && python -m pip check
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
